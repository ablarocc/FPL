#!/usr/bin/env python3
"""Read-only evaluation harness for the Bzzoiro Sports Data API.

The evaluator compares Bzzoiro with the checked-in FPL Core Insights data and
samples football-relevant enrichment and betting endpoints. It never writes
to ``data/`` or to Supabase. Output is limited to a caller-selected artifact directory outside `data/`.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import re
import subprocess
import ssl
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mapping  # noqa: E402


BASE_URL = "https://sports.bzzoiro.com"
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data"

OWNED_ARTIFACT_NAMES = {
    "comparison_events.json",
    "ENDPOINT_TELEMETRY.json",
    "event_identity_map.json",
    "event_identity_rejections.json",
    "friendly_event_identity_map.json",
    "friendlies_sample.json",
    "friendly_player_identity_map.json",
    "friendly_player_identity_rejections.json",
    "leagues.json",
    "mapped_betting_event.json",
    "odds_best_sample.json",
    "openapi_inventory.json",
    "overlap_field_inventory.json",
    "pl_seasons.json",
    "player_identity_map.json",
    "player_identity_rejections.json",
    "player_profile_sample.json",
    "predictions_sample.json",
    "README.md",
    "REPORT.md",
    "RUN_MANIFEST.json",
    "SECRET_SCAN_FAILED.txt",
    "SECRET_SCAN_OK",
    "SUMMARY.json",
    "team_identity_map.json",
    "team_identity_rejections.json",
}
OWNED_ARTIFACT_PATTERNS = (
    re.compile(r"player_stats_event_[A-Za-z0-9_-]+\.json"),
    re.compile(r"friendly_player_stats_event_[A-Za-z0-9_-]+\.json"),
    re.compile(r"friendly_event_[A-Za-z0-9_-]+_stats\.json"),
    re.compile(
        r"event_[A-Za-z0-9_-]+_(detail|stats|lineups|incidents|betting)\.json"
    ),
)

COMPETITION_RULES = {
    "Premier League": (r"^premier league$", "England"),
    "FA Cup": (r"^fa cup$", "England"),
    "EFL Cup": (r"^(efl|carabao|league) cup$", "England"),
    "Champions League": (r"^(uefa )?champions league$", "Europe"),
    "Europa League": (r"^(uefa )?europa league$", "Europe"),
    "Conference League": (r"^(uefa )?conference league$", "Europe"),
    "Community Shield": (r"community shield", "England"),
    "Club Friendlies": (r"club friendl", "World"),
}

CORE_STAT_THRESHOLDS = {
    "goals": 0.0,
    "total_shots": 0.0,
    "minutes_played": 1.5,
    "touches": 1.5,
    "xg": 0.03,
    "xa": 0.03,
}


@dataclass
class Check:
    check_id: str
    section: str
    status: str
    summary: str
    metrics: dict[str, Any] = field(default_factory=dict)


class Reporter:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.checks: list[Check] = []

    def say(self, line: str = "") -> None:
        print(line)
        self.lines.append(line)

    def section(self, title: str) -> None:
        self.say(f"## {title}\n")

    def check(
        self,
        check_id: str,
        section: str,
        status: str,
        summary: str,
        **metrics: Any,
    ) -> None:
        if status not in {"pass", "warn", "fail", "skip"}:
            raise ValueError(f"Invalid check status: {status}")
        self.checks.append(Check(check_id, section, status, summary, metrics))

    def render(self, api: "Api", args: argparse.Namespace) -> str:
        counts = {
            status: sum(c.status == status for c in self.checks)
            for status in ("pass", "warn", "fail", "skip")
        }
        rows = [
            "# Bzzoiro Sports Data — evaluation report",
            "",
            f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
            f"Base: `{api.base}` · repository: `{repository_name()}`",
            "",
            "## Executive summary",
            "",
            f"- **{counts['pass']} passed**, **{counts['warn']} warnings**, "
            f"**{counts['fail']} failed**, **{counts['skip']} skipped**.",
            f"- {api.ok_requests}/{api.logical_requests} logical API requests succeeded "
            f"({api.calls} HTTP attempts).",
            "- API calls were read-only. The evaluator wrote only the selected artifact directory; canonical `data/` and Supabase were unchanged.",
            "- Betting and provider availability are evaluated separately from canonical "
            "football/FPL facts.",
            "",
            "| Check | Status | Result |",
            "|---|---|---|",
        ]
        symbol = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
        for check in self.checks:
            rows.append(
                f"| `{check.check_id}` | **{symbol[check.status]}** | "
                f"{check.summary.replace('|', '/')} |"
            )
        rows.extend(["", *self.lines])
        return "\n".join(rows).rstrip() + "\n"

    def summary(self, api: "Api", args: argparse.Namespace) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "base_url": api.base,
            "configuration": {
                "comparison_season": args.comparison_season,
                "friendlies_season": args.friendlies_season,
                "gameweek": args.gameweek,
                "sample_matches": args.sample_matches,
                "friendly_sample": args.friendly_sample,
                "profile_sample": args.profile_sample,
            "identity_detail_limit": args.identity_detail_limit,
                "betting_enabled": not args.skip_betting,
            },
            "checks": [asdict(check) for check in self.checks],
            "api": api.telemetry_summary(),
        }


REPORTER = Reporter()


def warn(message: str) -> None:
    print(f"  ! {message}", file=sys.stderr)


class Api:
    """Small stdlib client with bounded retries and request telemetry."""

    def __init__(
        self,
        key: str,
        base: str = BASE_URL,
        delay: float = 0.1,
        timeout: float = 45,
    ) -> None:
        self.key = key
        self.base = base.rstrip("/")
        self.delay = delay
        self.timeout = timeout
        self.calls = 0
        self.logical_requests = 0
        self.ok_requests = 0
        self.dead = False
        self.transport_fails = 0
        self.telemetry: list[dict[str, Any]] = []
        self.ctx = ssl.create_default_context()
        bundle = os.environ.get("SSL_CERT_FILE") or "/root/.ccr/ca-bundle.crt"
        if os.path.exists(bundle):
            try:
                self.ctx.load_verify_locations(bundle)
            except Exception:
                pass

    def get(self, path: str, retries: int = 3, **params: Any) -> Any:
        self.logical_requests += 1
        if self.dead:
            self._record(path, params, 0, None, "host marked unreachable")
            return None
        qs = {key: value for key, value in params.items() if value is not None}
        url = f"{self.base}{path}"
        if qs:
            url += "?" + urllib.parse.urlencode(qs)
        started = time.perf_counter()
        final_status: int | None = None
        final_error: str | None = None
        attempts = 0

        for attempt in range(retries):
            attempts += 1
            self.calls += 1
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Token {self.key}",
                    "Accept": "application/json",
                    "User-Agent": "FPL-Core-Insights-evaluator/2.0",
                },
            )
            try:
                with urllib.request.urlopen(
                    req, timeout=self.timeout, context=self.ctx
                ) as response:
                    final_status = response.status
                    payload = json.loads(response.read().decode("utf-8"))
                self.transport_fails = 0
                self.ok_requests += 1
                time.sleep(self.delay)
                self._record(
                    path,
                    params,
                    attempts,
                    final_status,
                    None,
                    started=started,
                )
                return payload
            except urllib.error.HTTPError as exc:
                final_status = exc.code
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                final_error = f"HTTP {exc.code}: {body}".strip()
                if exc.code in {401, 403}:
                    self.dead = True
                    break
                if exc.code == 404:
                    break
                warn(f"HTTP {exc.code} on {path} (attempt {attempts}/{retries})")
            except Exception as exc:
                final_error = f"{type(exc).__name__}: {exc}"
                self.transport_fails += 1
                warn(f"{final_error} on {path} (attempt {attempts}/{retries})")
                if self.transport_fails >= retries and self.ok_requests == 0:
                    self.dead = True
                    break
            if attempt + 1 < retries:
                time.sleep(2**attempt)

        self._record(
            path,
            params,
            attempts,
            final_status,
            final_error,
            started=started,
        )
        return None

    def paged(
        self,
        path: str,
        cap: int = 1000,
        envelope_keys: tuple[str, ...] = (),
        **params: Any,
    ) -> list[dict]:
        out: list[dict] = []
        offset = 0
        limit = min(200, cap)
        while len(out) < cap:
            data = self.get(path, limit=limit, offset=offset, **params)
            rows = response_rows(data, *envelope_keys)
            if not rows:
                break
            out.extend(rows)
            if isinstance(data, dict) and data.get("next"):
                offset += limit
                continue
            if len(rows) < limit:
                break
            offset += limit
        return out[:cap]

    def _record(
        self,
        path: str,
        params: dict[str, Any],
        attempts: int,
        status: int | None,
        error: str | None,
        started: float | None = None,
    ) -> None:
        elapsed = (
            round((time.perf_counter() - started) * 1000, 1)
            if started is not None
            else 0.0
        )
        self.telemetry.append(
            {
                "path": path,
                "params": params,
                "attempts": attempts,
                "status": status,
                "elapsed_ms": elapsed,
                "ok": status is not None and 200 <= status < 300,
                "error": error,
            }
        )

    def telemetry_summary(self) -> dict[str, Any]:
        elapsed = [row["elapsed_ms"] for row in self.telemetry if row["elapsed_ms"]]
        sorted_elapsed = sorted(elapsed)
        p95 = (
            sorted_elapsed[min(len(sorted_elapsed) - 1, int(len(sorted_elapsed) * 0.95))]
            if sorted_elapsed
            else None
        )
        return {
            "logical_requests": self.logical_requests,
            "http_attempts": self.calls,
            "successful_requests": self.ok_requests,
            "success_rate": (
                round(self.ok_requests / self.logical_requests, 4)
                if self.logical_requests
                else 0
            ),
            "median_ms": round(statistics.median(elapsed), 1) if elapsed else None,
            "p95_ms": p95,
            "errors": [row for row in self.telemetry if not row["ok"]],
        }


def response_rows(data: Any, *keys: str) -> list:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in (*keys, "results"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def number(value: Any) -> float | None:
    try:
        if value in ("", None, "None"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def id_key(value: Any) -> str:
    numeric = number(value)
    if numeric is not None and numeric.is_integer():
        return str(int(numeric))
    return str(value or "")


def norm(value: Any) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def surname(value: Any) -> str:
    text = norm(value)
    return text.split()[-1] if text else ""


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def artifact_path(out: Path, name: str) -> Path:
    """Resolve a flat, conservative artifact name beneath the output root."""
    raw_name = str(name)
    if (
        raw_name in {"", ".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", raw_name)
    ):
        raise ValueError(f"Unsafe artifact filename: {raw_name!r}")
    root = out.resolve()
    target = (root / raw_name).resolve()
    if target.parent != root:
        raise ValueError(f"Artifact filename escapes output directory: {raw_name!r}")
    return target


def dump_json(out: Path, name: str, payload: Any) -> None:
    out.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    artifact_path(out, name).write_text(text + "\n", encoding="utf-8")


def clear_owned_artifacts(out: Path) -> None:
    """Remove only evaluator-owned files so a refresh cannot mix old evidence."""
    for path in out.iterdir():
        if not path.is_file():
            continue
        owned = path.name in OWNED_ARTIFACT_NAMES or any(
            pattern.fullmatch(path.name) for pattern in OWNED_ARTIFACT_PATTERNS
        )
        if owned:
            path.unlink()


def safe_output_path(raw: str) -> Path:
    out = Path(raw).expanduser().resolve()
    data = DATA_ROOT.resolve()
    repo = REPO_ROOT.resolve()
    runtime_root = (repo / "bzzoiro_probe_out").resolve()
    review_snapshot = (repo / "scripts" / "bzzoiro" / "sample_data").resolve()
    if out == data or data in out.parents:
        raise ValueError("Refusing to write probe artifacts inside the repository data/ tree")
    if out == repo:
        raise ValueError("Refusing to use the repository root as an artifact directory")
    if repo in out.parents:
        runtime_output = out == runtime_root or runtime_root in out.parents
        if not runtime_output and out != review_snapshot:
            raise ValueError(
                "Repository outputs are limited to bzzoiro_probe_out/ or the "
                "intentional scripts/bzzoiro/sample_data snapshot"
            )
    return out


def ensure_owned_output_directory(out: Path) -> None:
    """Refuse cleanup in a non-empty directory not owned by this evaluator."""
    if not out.exists():
        return
    if not out.is_dir():
        raise ValueError(f"Artifact output is not a directory: {out}")
    if not any(out.iterdir()):
        return
    marker = out / "RUN_MANIFEST.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict) or payload.get("probe_version") != "2.0":
        raise ValueError(
            "Refusing to clean a non-empty artifact directory without an "
            f"evaluator ownership marker: {out}"
        )


def select_competition(
    leagues: list[dict], pattern: str, expected_country: str
) -> dict | None:
    candidates = [
        league
        for league in leagues
        if re.search(pattern, str(league.get("name", "")), re.I)
    ]
    preferred = [
        league
        for league in candidates
        if str(league.get("country", "")).casefold() == expected_country.casefold()
    ]
    return preferred[0] if preferred else None


def evaluate_schema(schema_path: str | None, out: Path) -> None:
    REPORTER.section("1. OpenAPI and static column coverage")
    coverage = mapping.coverage_summary()
    REPORTER.say(
        f"- Existing `playermatchstats.csv`: **{coverage['repo_columns']}** columns "
        f"(**{coverage['identity_columns']} identity** + **{coverage['stat_columns']} statistics**)."
    )
    REPORTER.say(
        f"- Schema-comparable candidates: **{coverage['direct']} direct** + "
        f"**{coverage['derived']} derived** = **{coverage['candidate_comparable']}**."
    )
    REPORTER.say(
        f"- Current merge allowlist: **{coverage['merge_safe']} direct observed fields**; "
        "derived fields and unobserved `penalty_miss` remain evaluation-only."
    )
    REPORTER.say(
        f"- Still unavailable: **{coverage['unavailable']}** existing statistics."
    )
    REPORTER.say(
        f"- Additional player-match fields: **{coverage['new_fields']}**.\n"
    )
    REPORTER.check(
        "static.player_match_coverage",
        "schema",
        "warn",
        f"{coverage['merge_safe']}/{coverage['stat_columns']} merge-safe; "
        f"{coverage['candidate_comparable']} schema-comparable candidates",
        **coverage,
    )

    if not schema_path:
        REPORTER.say(
            "No `--schema` file supplied; live tests continue without an OpenAPI inventory.\n"
        )
        REPORTER.check(
            "schema.openapi_inventory",
            "schema",
            "skip",
            "No optional OpenAPI file supplied",
        )
        return

    path = Path(schema_path).expanduser()
    if not path.exists():
        REPORTER.say(f"OpenAPI file not found: `{path}`.\n")
        REPORTER.check(
            "schema.openapi_inventory",
            "schema",
            "warn",
            "Requested OpenAPI file was not found",
        )
        return

    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        REPORTER.say(f"Could not parse `{path}`: {exc}\n")
        REPORTER.check(
            "schema.openapi_inventory",
            "schema",
            "fail",
            "OpenAPI file could not be parsed",
        )
        return

    paths = schema.get("paths", {})
    schemas = schema.get("components", {}).get("schemas", {})
    methods = [
        (method.upper(), endpoint, operation.get("tags", []))
        for endpoint, operations in paths.items()
        for method, operation in operations.items()
        if method.lower() in {"get", "post", "put", "patch", "delete"}
        and isinstance(operation, dict)
    ]
    betting = [
        {"method": method, "path": endpoint, "tags": tags}
        for method, endpoint, tags in methods
        if re.search(r"odds|bookmaker|prediction|polymarket", endpoint, re.I)
    ]
    summary = {
        "openapi": schema.get("openapi"),
        "title": schema.get("info", {}).get("title"),
        "version": schema.get("info", {}).get("version"),
        "paths": len(paths),
        "operations": len(methods),
        "schemas": len(schemas),
        "betting_operations": betting,
    }
    dump_json(out, "openapi_inventory.json", summary)
    REPORTER.say(
        f"OpenAPI `{summary['version']}` describes **{len(paths)} paths**, "
        f"**{len(methods)} operations**, and **{len(schemas)} schemas**."
    )
    REPORTER.say(
        f"Betting/prediction operations identified: **{len(betting)}**.\n"
    )
    REPORTER.check(
        "schema.openapi_inventory",
        "schema",
        "pass",
        f"{len(paths)} paths and {len(schemas)} schemas parsed",
        **summary,
    )


def evaluate_competitions(api: Api, out: Path) -> dict[str, dict]:
    REPORTER.section("2. Competition coverage")
    leagues = api.paged("/api/v2/leagues/", cap=1000)
    dump_json(out, "leagues.json", leagues)
    REPORTER.say(f"`/api/v2/leagues/` returned **{len(leagues)}** competitions.\n")
    REPORTER.say("| Competition | Expected region | Result |")
    REPORTER.say("|---|---|---|")

    found: dict[str, dict] = {}
    for label, (pattern, country) in COMPETITION_RULES.items():
        league = select_competition(leagues, pattern, country)
        if league:
            found[label] = league
            REPORTER.say(
                f"| {label} | {country} | `{league.get('id')}` "
                f"{league.get('name')} ({league.get('country')}) |"
            )
        else:
            REPORTER.say(f"| {label} | {country} | **not found** |")
    REPORTER.say("")

    essential = {
        "Premier League",
        "FA Cup",
        "EFL Cup",
        "Champions League",
        "Europa League",
        "Conference League",
        "Club Friendlies",
    }
    missing = sorted(essential - set(found))
    REPORTER.check(
        "coverage.competitions",
        "coverage",
        "pass" if not missing else "fail",
        "All relevant competitions found"
        if not missing
        else f"Missing: {', '.join(missing)}",
        found={key: value.get("id") for key, value in found.items()},
        missing=missing,
    )
    REPORTER.check(
        "coverage.community_shield",
        "coverage",
        "pass" if "Community Shield" in found else "warn",
        "Community Shield found"
        if "Community Shield" in found
        else "Community Shield not listed as a distinct competition",
    )
    return found


def evaluate_seasons(api: Api, league: dict, out: Path) -> dict[str, dict]:
    REPORTER.section("3. Premier League historical depth")
    data = api.get(f"/api/v2/leagues/{league['id']}/seasons/")
    seasons = response_rows(data, "seasons")
    dump_json(out, "pl_seasons.json", seasons)
    REPORTER.say(f"Premier League seasons returned: **{len(seasons)}**.\n")
    REPORTER.say("| ID | Season | Year | Current |")
    REPORTER.say("|---|---|---|---|")
    for season in seasons[:8]:
        REPORTER.say(
            f"| `{season.get('id')}` | {season.get('name')} | "
            f"{season.get('year')} | {season.get('is_current')} |"
        )
    REPORTER.say("")

    resolved: dict[str, dict] = {}
    for season in seasons:
        year = number(season.get("year"))
        if year is None:
            continue
        start = int(year)
        resolved.setdefault(f"{start}-{start + 1}", season)
    oldest = min((int(s.get("year")) for s in seasons if s.get("year")), default=None)
    REPORTER.check(
        "coverage.history",
        "coverage",
        "pass" if len(seasons) >= 10 else "warn",
        f"{len(seasons)} Premier League seasons; oldest starts {oldest}",
        count=len(seasons),
        oldest_start_year=oldest,
    )
    return resolved


def _repo_player_indexes(players: list[dict]) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    by_full: dict[str, dict] = {}
    by_surname: dict[str, list[dict]] = defaultdict(list)
    for player in players:
        full = norm(f"{player.get('first_name', '')} {player.get('second_name', '')}")
        web = norm(player.get("web_name"))
        if full:
            by_full.setdefault(full, player)
        if web:
            by_full.setdefault(web, player)
        last = surname(full or web)
        if last:
            by_surname[last].append(player)
    return by_full, by_surname


def _match_repo_player_with_method(
    provider: dict,
    by_full: dict[str, dict],
    by_surname: dict[str, list[dict]],
) -> tuple[dict | None, str | None]:
    """Return the canonical player and the conservative name-match method."""
    name = norm(provider.get("name") or provider.get("short_name"))
    if name in by_full:
        return by_full[name], "full_name"
    candidates = by_surname.get(surname(name), [])
    if len(candidates) == 1:
        return candidates[0], "unique_surname"
    return None, None


def _match_repo_player(
    provider: dict,
    by_full: dict[str, dict],
    by_surname: dict[str, list[dict]],
) -> dict | None:
    player, _ = _match_repo_player_with_method(provider, by_full, by_surname)
    return player


def identity_sort_key(value: Any) -> tuple[int, Any]:
    """Sort numeric provider IDs naturally while retaining opaque string IDs."""
    key = id_key(value)
    try:
        return 0, int(key)
    except ValueError:
        return 1, key


def derived_provider_value(row: dict, repo_field: str) -> float | None:
    first_field, second_field = mapping.PMS_DERIVED[repo_field]
    first = number(row.get(first_field))
    second = number(row.get(second_field))
    if first is None or second is None:
        return None
    if repo_field in {"duels_won", "duels_lost"}:
        return first + second
    denominator = (
        first + second
        if repo_field in {"ground_duels_won_percent", "aerial_duels_won_percent"}
        else second
    )
    if denominator == 0:
        return 0.0
    return first / denominator * 100


def evaluate_overlap(
    api: Api,
    league: dict,
    season: dict,
    season_label: str,
    gameweek: int,
    sample_matches: int,
    identity_detail_limit: int,
    out: Path,
) -> dict[str, Any]:
    REPORTER.section(
        f"4. Existing-data cross-check ({season_label}, GW{gameweek})"
    )
    repo_dir = (
        DATA_ROOT
        / season_label
        / "By Tournament"
        / "Premier League"
        / f"GW{gameweek}"
    )
    repo_matches = read_csv(repo_dir / "matches.csv")
    repo_stats = read_csv(repo_dir / "playermatchstats.csv")
    repo_players = read_csv(DATA_ROOT / season_label / "players.csv")
    repo_team_rows = read_csv(DATA_ROOT / season_label / "teams.csv")
    repo_teams = {
        id_key(team.get("code")): team
        for team in repo_team_rows
    }
    repo_teams_by_name = {
        str(team.get("name")): team for team in repo_team_rows if team.get("name")
    }
    if not repo_matches:
        REPORTER.say(f"No repository data found at `{repo_dir}`.\n")
        REPORTER.check(
            "overlap.repo_data",
            "overlap",
            "fail",
            f"No comparison data for GW{gameweek}",
        )
        return {}

    events = api.paged(
        "/api/v2/events/",
        cap=1000,
        league_id=league.get("id"),
        season_id=season.get("id"),
    )
    dump_json(out, "comparison_events.json", events)
    REPORTER.say(
        f"Repository: **{len(repo_matches)} matches**, **{len(repo_stats)} player rows**."
    )
    REPORTER.say(f"API season: **{len(events)} events**.\n")

    by_pair: dict[tuple[str, str], dict] = {}
    unresolved_names: set[str] = set()
    team_identity_by_provider: dict[str, dict[str, Any]] = {}
    team_rejections_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in events:
        raw_home = event.get("home_team") or ""
        raw_away = event.get("away_team") or ""
        home = mapping.resolve_team_name(raw_home)
        away = mapping.resolve_team_name(raw_away)
        if home and away:
            by_pair[(home, away)] = event
        else:
            unresolved_names.update(
                value
                for value, mapped in ((raw_home, home), (raw_away, away))
                if value and not mapped
            )
        for side, raw_name, canonical_name in (
            ("home", raw_home, home),
            ("away", raw_away, away),
        ):
            provider_team_id = id_key(event.get(f"{side}_team_id"))
            canonical_team = repo_teams_by_name.get(str(canonical_name))
            if not provider_team_id or not canonical_team:
                reason = (
                    "missing_provider_team_id"
                    if not provider_team_id
                    else "unresolved_canonical_team"
                )
                key = (provider_team_id, str(raw_name), reason)
                team_rejections_by_key.setdefault(
                    key,
                    {
                        "provider_team_id": provider_team_id or None,
                        "provider_name": raw_name or None,
                        "reason": reason,
                    },
                )
                continue
            identity = {
                "provider_team_id": event.get(f"{side}_team_id"),
                "provider_name": raw_name,
                "fpl_team_id": canonical_team.get("id"),
                "fpl_team_code": canonical_team.get("code"),
                "fpl_name": canonical_team.get("name"),
                "match_method": "canonical_alias",
                "confidence": "high",
            }
            existing = team_identity_by_provider.get(provider_team_id)
            if existing and id_key(existing.get("fpl_team_code")) != id_key(
                canonical_team.get("code")
            ):
                key = (provider_team_id, str(raw_name), "conflicting_canonical_team")
                team_rejections_by_key[key] = {
                    **identity,
                    "reason": "conflicting_canonical_team",
                    "existing_fpl_team_code": existing.get("fpl_team_code"),
                }
                continue
            team_identity_by_provider[provider_team_id] = identity

    team_identity_rows = sorted(
        team_identity_by_provider.values(),
        key=lambda row: identity_sort_key(row.get("provider_team_id")),
    )
    team_identity_rejections = sorted(
        team_rejections_by_key.values(),
        key=lambda row: (
            identity_sort_key(row.get("provider_team_id")),
            str(row.get("provider_name") or ""),
        ),
    )
    dump_json(out, "team_identity_map.json", team_identity_rows)
    dump_json(out, "team_identity_rejections.json", team_identity_rejections)

    paired: list[tuple[dict, dict]] = []
    unpaired: list[str] = []
    event_identity_rows: list[dict[str, Any]] = []
    event_identity_rejections: list[dict[str, Any]] = []
    for match in repo_matches:
        home_team = repo_teams.get(id_key(match.get("home_team")), {})
        away_team = repo_teams.get(id_key(match.get("away_team")), {})
        home = home_team.get("name")
        away = away_team.get("name")
        event = by_pair.get((home, away))
        if event:
            paired.append((match, event))
            event_identity_rows.append(
                {
                    "provider_event_id": event.get("id"),
                    "fpl_match_id": match.get("match_id"),
                    "provider_home_team_id": event.get("home_team_id"),
                    "provider_away_team_id": event.get("away_team_id"),
                    "fpl_home_team_code": home_team.get("code"),
                    "fpl_away_team_code": away_team.get("code"),
                    "provider_kickoff": event.get("event_date"),
                    "fpl_kickoff": match.get("kickoff_time"),
                    "match_method": "canonical_home_away_pair",
                    "confidence": "high",
                }
            )
        else:
            fixture = f"{home} v {away}"
            unpaired.append(fixture)
            event_identity_rejections.append(
                {
                    "fpl_match_id": match.get("match_id"),
                    "fpl_fixture": fixture,
                    "reason": "no_provider_event_for_canonical_home_away_pair",
                }
            )
    dump_json(
        out,
        "event_identity_map.json",
        sorted(event_identity_rows, key=lambda row: str(row.get("fpl_match_id") or "")),
    )
    dump_json(out, "event_identity_rejections.json", event_identity_rejections)

    score_matches = sum(
        number(match.get("home_score")) is not None
        and number(match.get("away_score")) is not None
        and number(event.get("home_score")) is not None
        and number(event.get("away_score")) is not None
        and number(match.get("home_score")) == number(event.get("home_score"))
        and number(match.get("away_score")) == number(event.get("away_score"))
        for match, event in paired
    )
    kickoff_deltas: list[float] = []
    for match, event in paired:
        try:
            left = datetime.fromisoformat(str(match["kickoff_time"]).replace("Z", "+00:00"))
            right = datetime.fromisoformat(str(event["event_date"]).replace("Z", "+00:00"))
            if left.tzinfo is None:
                left = left.replace(tzinfo=timezone.utc)
            if right.tzinfo is None:
                right = right.replace(tzinfo=timezone.utc)
            kickoff_deltas.append(abs((left - right).total_seconds()) / 60)
        except (KeyError, TypeError, ValueError):
            pass

    REPORTER.say(f"**Fixture mapping:** {len(paired)}/{len(repo_matches)}.")
    REPORTER.say(f"**Score agreement:** {score_matches}/{len(paired)}.")
    if kickoff_deltas:
        REPORTER.say(
            f"**Kickoff agreement:** median delta "
            f"{statistics.median(kickoff_deltas):.1f} minutes."
        )
    if unpaired:
        REPORTER.say(f"Unmatched examples: {', '.join(unpaired[:5])}.")
    if unresolved_names:
        REPORTER.say(
            f"Unresolved provider aliases: {', '.join(sorted(unresolved_names)[:8])}."
        )
    REPORTER.say("")

    fixture_rate = len(paired) / len(repo_matches)
    score_rate = score_matches / len(paired) if paired else 0
    REPORTER.check(
        "overlap.fixture_mapping",
        "overlap",
        "pass" if fixture_rate == 1 else ("warn" if fixture_rate >= 0.8 else "fail"),
        f"{len(paired)}/{len(repo_matches)} fixtures mapped",
        mapped=len(paired),
        total=len(repo_matches),
        rate=fixture_rate,
        unmatched=unpaired,
    )
    REPORTER.check(
        "overlap.scores",
        "overlap",
        "pass" if score_rate == 1 else "fail",
        f"{score_matches}/{len(paired)} mapped scores agree",
        agreed=score_matches,
        total=len(paired),
        rate=score_rate,
    )

    sample = paired[:sample_matches]
    repo_by_match: dict[str, list[dict]] = defaultdict(list)
    for row in repo_stats:
        repo_by_match[str(row.get("match_id"))].append(row)

    provider_players: dict[str, dict] = {}
    for team_id in {
        id_key(event.get(key))
        for _, event in sample
        for key in ("home_team_id", "away_team_id")
        if event.get(key) is not None
    }:
        squad = api.get(f"/api/v2/teams/{team_id}/squad/") or {}
        for player in response_rows(squad, "players"):
            provider_players[id_key(player.get("id"))] = player

    # Live historical lineups currently contain names but null player IDs.
    # Record that drift; bounded player-detail calls fill historical names
    # absent from current squads without turning this into a bulk importer.
    lineup_rows = lineup_rows_with_ids = 0
    for _, event in sample:
        lineup = api.get(f"/api/v2/events/{event['id']}/lineups/") or {}
        for side in ("home", "away"):
            side_data = (lineup.get("lineups") or {}).get(side) or {}
            for key in ("players", "substitutes"):
                for player in side_data.get(key) or []:
                    lineup_rows += 1
                    lineup_rows_with_ids += player.get("id") is not None
    detail_attempted: set[str] = set()
    detail_fetched = 0

    player_total = 0
    player_mapped = 0
    deltas: dict[str, list[float]] = defaultdict(list)
    exact: dict[str, int] = defaultdict(int)
    observed_fields: set[str] = set()
    field_profile: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"rows": 0, "populated": 0, "types": set()}
    )
    mapped_examples: list[dict[str, Any]] = []
    player_identity_by_provider: dict[str, dict[str, Any]] = {}
    player_identity_rejections: list[dict[str, Any]] = []

    for match, event in sample:
        payload = api.get(f"/api/v2/events/{event['id']}/player-stats/")
        rows = response_rows(payload, "player_stats")
        dump_json(out, f"player_stats_event_{event['id']}.json", rows)
        ours = repo_by_match.get(str(match.get("match_id")), [])
        ours_by_full, ours_by_surname = _repo_player_indexes(
            [
                {
                    **player,
                    **row,
                }
                for row in ours
                for player in repo_players
                if id_key(player.get("player_id")) == id_key(row.get("player_id"))
            ]
        )
        for row in rows:
            observed_fields.update(row)
            for field_name, value in row.items():
                field_profile[field_name]["rows"] += 1
                if value is not None:
                    field_profile[field_name]["populated"] += 1
                    field_profile[field_name]["types"].add(type(value).__name__)
            player_total += 1
            provider_id = id_key(row.get("player_id"))
            provider = provider_players.get(provider_id, {})
            if (
                not provider
                and provider_id not in detail_attempted
                and detail_fetched < identity_detail_limit
            ):
                detail_attempted.add(provider_id)
                detail = api.get(f"/api/v2/players/{provider_id}/") or {}
                detail_fetched += 1
                if detail:
                    provider_players[provider_id] = detail
                    provider = detail
            ours_row, match_method = _match_repo_player_with_method(
                provider, ours_by_full, ours_by_surname
            )
            if not ours_row:
                player_identity_rejections.append(
                    {
                        "provider_player_id": row.get("player_id"),
                        "provider_name": provider.get("name") or provider.get("short_name"),
                        "provider_team_id": row.get("team_id"),
                        "provider_event_id": event.get("id"),
                        "fpl_match_id": match.get("match_id"),
                        "reason": (
                            "provider_identity_unavailable"
                            if not provider
                            else "no_canonical_player_name_match"
                        ),
                    }
                )
                continue
            identity = {
                "provider_player_id": row.get("player_id"),
                "provider_name": provider.get("name") or provider.get("short_name"),
                "provider_team_id": row.get("team_id"),
                "fpl_player_id": ours_row.get("player_id"),
                "fpl_name": ours_row.get("web_name"),
                "fpl_team_code": ours_row.get("team_code"),
                "match_method": match_method,
                "confidence": "high" if match_method == "full_name" else "medium",
                "provider_event_ids": [event.get("id")],
                "fpl_match_ids": [match.get("match_id")],
            }
            existing = player_identity_by_provider.get(provider_id)
            if existing and id_key(existing.get("fpl_player_id")) != id_key(
                ours_row.get("player_id")
            ):
                player_identity_rejections.append(
                    {
                        **identity,
                        "reason": "conflicting_canonical_player",
                        "existing_fpl_player_id": existing.get("fpl_player_id"),
                    }
                )
                continue
            if existing:
                if event.get("id") not in existing["provider_event_ids"]:
                    existing["provider_event_ids"].append(event.get("id"))
                if match.get("match_id") not in existing["fpl_match_ids"]:
                    existing["fpl_match_ids"].append(match.get("match_id"))
            else:
                player_identity_by_provider[provider_id] = identity
            player_mapped += 1
            if len(mapped_examples) < 10:
                mapped_examples.append(
                    {
                        "provider_player_id": row.get("player_id"),
                        "provider_name": provider.get("name"),
                        "fpl_player_id": ours_row.get("player_id"),
                        "fpl_name": ours_row.get("web_name"),
                        "match_method": match_method,
                    }
                )
            for provider_field, repo_field in mapping.PMS_DIRECT.items():
                left = number(row.get(provider_field))
                right = number(ours_row.get(repo_field))
                if left is None or right is None:
                    continue
                delta = abs(left - right)
                deltas[repo_field].append(delta)
                if delta < 1e-6:
                    exact[repo_field] += 1
            for repo_field in mapping.PMS_DERIVED:
                left = derived_provider_value(row, repo_field)
                right = number(ours_row.get(repo_field))
                if left is None or right is None:
                    continue
                delta = abs(left - right)
                deltas[repo_field].append(delta)
                if delta < 1e-6:
                    exact[repo_field] += 1

    player_identity_rows = sorted(
        player_identity_by_provider.values(),
        key=lambda row: identity_sort_key(row.get("provider_player_id")),
    )
    for identity in player_identity_rows:
        identity["provider_event_ids"] = sorted(
            identity["provider_event_ids"], key=identity_sort_key
        )
        identity["fpl_match_ids"] = sorted(
            identity["fpl_match_ids"], key=lambda value: str(value or "")
        )
    player_identity_rejections.sort(
        key=lambda row: (
            identity_sort_key(row.get("provider_player_id")),
            identity_sort_key(row.get("provider_event_id")),
            str(row.get("reason") or ""),
        )
    )
    dump_json(out, "player_identity_map.json", player_identity_rows)
    dump_json(out, "player_identity_rejections.json", player_identity_rejections)

    serial_field_profile = {
        field_name: {
            "rows": values["rows"],
            "populated": values["populated"],
            "population_rate": (
                values["populated"] / values["rows"] if values["rows"] else 0
            ),
            "types": sorted(values["types"]),
        }
        for field_name, values in sorted(field_profile.items())
    }
    mapped_source_fields = set(mapping.PMS_DIRECT) | {
        source
        for pair in mapping.PMS_DERIVED.values()
        for source in pair
    }
    missing_mapped_fields = sorted(mapped_source_fields - observed_fields)
    unmapped_observed_fields = sorted(
        observed_fields - mapped_source_fields - {"id", "player_id", "event_id", "team_id"}
    )

    dump_json(
        out,
        "overlap_field_inventory.json",
        {
            "observed_player_stat_fields": sorted(observed_fields),
            "field_profile": serial_field_profile,
            "mapped_fields_missing_from_live_sample": missing_mapped_fields,
            "unmapped_live_fields": unmapped_observed_fields,
            "mapped_player_examples": mapped_examples,
        },
    )
    REPORTER.say(
        f"**Player mapping:** {player_mapped}/{player_total} sampled API rows "
        f"mapped to canonical FPL players."
    )
    REPORTER.say(
        "Mapping uses full names first and unique surnames only as a fallback; "
        "it is diagnostic, not a production identity strategy."
    )
    REPORTER.say(
        f"Historical identity detail fallback: **{detail_fetched}** bounded lookups. "
        f"Lineup IDs populated: **{lineup_rows_with_ids}/{lineup_rows}**."
    )
    REPORTER.say(
        f"Observed-field drift: **{len(missing_mapped_fields)} mapped source fields absent** "
        f"from the sample; **{len(unmapped_observed_fields)} live fields unmapped**.\n"
    )
    REPORTER.say("| Existing column | n | Mean absolute difference | Exact |")
    REPORTER.say("|---|---:|---:|---:|")
    stat_metrics: dict[str, Any] = {}
    for column in sorted(deltas):
        values = deltas[column]
        mean = sum(values) / len(values)
        stat_metrics[column] = {
            "n": len(values),
            "mean_absolute_difference": mean,
            "exact": exact[column],
            "exact_rate": exact[column] / len(values),
        }
        REPORTER.say(
            f"| `{column}` | {len(values)} | {mean:.4f} | "
            f"{exact[column]}/{len(values)} |"
        )
    REPORTER.say("")

    mapping_rate = player_mapped / player_total if player_total else 0
    REPORTER.check(
        "overlap.player_mapping",
        "overlap",
        "pass" if mapping_rate >= 0.8 else ("warn" if mapping_rate >= 0.5 else "fail"),
        f"{player_mapped}/{player_total} sampled player rows mapped",
        mapped=player_mapped,
        total=player_total,
        rate=mapping_rate,
    )
    REPORTER.check(
        "overlap.lineup_identifiers",
        "overlap",
        "pass" if lineup_rows and lineup_rows_with_ids == lineup_rows else "warn",
        f"{lineup_rows_with_ids}/{lineup_rows} historical lineup rows carry player IDs",
        rows=lineup_rows,
        populated_ids=lineup_rows_with_ids,
        detail_fallback_calls=detail_fetched,
    )
    REPORTER.check(
        "overlap.response_drift",
        "overlap",
        "pass" if not missing_mapped_fields else "warn",
        f"{len(missing_mapped_fields)} mapped fields absent; "
        f"{len(unmapped_observed_fields)} live fields unmapped",
        missing_mapped_fields=missing_mapped_fields,
        unmapped_observed_fields=unmapped_observed_fields,
    )
    for column, threshold in CORE_STAT_THRESHOLDS.items():
        metric = stat_metrics.get(column)
        if not metric:
            REPORTER.check(
                f"overlap.stat.{column}",
                "overlap",
                "warn",
                "No comparable non-null values",
            )
            continue
        mean = metric["mean_absolute_difference"]
        REPORTER.check(
            f"overlap.stat.{column}",
            "overlap",
            "pass" if mean <= threshold else "warn",
            f"mean absolute difference {mean:.4f} (threshold {threshold})",
            **metric,
            threshold=threshold,
        )

    context = {
        "sample": sample,
        "sample_event": sample[0][1] if sample else None,
        "provider_players": provider_players,
    }
    return context


def evaluate_shots(api: Api, event: dict | None, out: Path) -> None:
    REPORTER.section("5. Shot maps and event-level statistics")
    if not event:
        REPORTER.say("No matched event available for shot-map testing.\n")
        REPORTER.check(
            "enrichment.shotmap", "enrichment", "skip", "No matched event available"
        )
        return
    payload = api.get(f"/api/v2/events/{event['id']}/stats/")
    dump_json(out, f"event_{event['id']}_stats.json", payload or {})
    shots = (payload or {}).get("shotmap") or []
    with_player = sum(
        bool(shot.get("player_id") or shot.get("pid")) for shot in shots
    )
    with_xg = sum(number(shot.get("xg")) is not None for shot in shots)
    with_xgot = sum(number(shot.get("xgot")) is not None for shot in shots)
    with_coords = sum(isinstance(shot.get("pos"), dict) for shot in shots)
    REPORTER.say(
        f"Event `{event['id']}` returned **{len(shots)} shots**: "
        f"{with_player} with player IDs, {with_xg} with xG, "
        f"{with_xgot} with xGOT, {with_coords} with pitch coordinates.\n"
    )
    REPORTER.say(
        f"Other event-stat families present: "
        f"`momentum={bool((payload or {}).get('momentum'))}`, "
        f"`xg_per_minute={bool((payload or {}).get('xg_per_minute'))}`, "
        f"`average_positions={bool((payload or {}).get('average_positions'))}`.\n"
    )
    join_rate = with_player / len(shots) if shots else 0
    REPORTER.check(
        "enrichment.shotmap",
        "enrichment",
        "pass" if shots and join_rate >= 0.9 else ("warn" if shots else "fail"),
        f"{with_player}/{len(shots)} shots carry a player ID",
        event_id=event.get("id"),
        shots=len(shots),
        with_player_id=with_player,
        with_xg=with_xg,
        with_xgot=with_xgot,
        with_coordinates=with_coords,
    )


def resolve_current_teams(api: Api, season_label: str) -> tuple[list[dict], dict[str, dict]]:
    teams = read_csv(DATA_ROOT / season_label / "teams.csv")
    resolved: dict[str, dict] = {}
    for team in teams:
        aliases = mapping.TEAM_ALIASES.get(team.get("name"), [team.get("name", "")])
        match = None
        for query in sorted(aliases, key=len, reverse=True):
            payload = api.get("/api/v2/teams/", name=query, limit=20)
            for candidate in response_rows(payload, "teams"):
                if mapping.resolve_team_name(candidate.get("name", "")) == team.get("name"):
                    match = candidate
                    break
            if match:
                break
        if match:
            resolved[id_key(team.get("id"))] = match
    return teams, resolved


def build_friendly_player_identity_map(
    api: Api,
    teams: list[dict],
    resolved_teams: dict[str, dict],
    season_label: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build a season-scoped provider-to-FPL player bridge from current squads."""
    fpl_players = read_csv(DATA_ROOT / season_label / "players.csv")
    players_by_team_code: dict[str, list[dict]] = defaultdict(list)
    for player in fpl_players:
        players_by_team_code[id_key(player.get("team_code"))].append(player)

    identity_by_provider: dict[str, dict[str, Any]] = {}
    rejections_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for team in sorted(teams, key=lambda row: identity_sort_key(row.get("code"))):
        provider_team = resolved_teams.get(id_key(team.get("id")))
        if not provider_team:
            continue
        canonical_players = players_by_team_code.get(id_key(team.get("code")), [])
        by_full, by_surname = _repo_player_indexes(canonical_players)
        squad = api.get(f"/api/v2/teams/{provider_team['id']}/squad/") or {}
        for provider in response_rows(squad, "players"):
            provider_id = id_key(provider.get("id"))
            canonical, method = _match_repo_player_with_method(
                provider, by_full, by_surname
            )
            if not provider_id or not canonical:
                reason = (
                    "missing_provider_player_id"
                    if not provider_id
                    else "no_canonical_player_name_match_within_team"
                )
                key = (provider_id, str(provider.get("name") or ""), reason)
                rejections_by_key.setdefault(
                    key,
                    {
                        "season": season_label,
                        "provider_player_id": provider.get("id"),
                        "provider_name": provider.get("name") or provider.get("short_name"),
                        "provider_team_id": provider_team.get("id"),
                        "provider_team_name": provider_team.get("name"),
                        "fpl_team_code": team.get("code"),
                        "reason": reason,
                    },
                )
                continue
            identity = {
                "season": season_label,
                "provider_player_id": provider.get("id"),
                "provider_name": provider.get("name") or provider.get("short_name"),
                "provider_team_id": provider_team.get("id"),
                "provider_team_name": provider_team.get("name"),
                "fpl_player_id": canonical.get("player_id"),
                "fpl_name": canonical.get("web_name"),
                "fpl_team_code": canonical.get("team_code"),
                "match_method": method,
                "confidence": "high" if method == "full_name" else "medium",
            }
            existing = identity_by_provider.get(provider_id)
            if existing and id_key(existing.get("fpl_player_id")) != id_key(
                canonical.get("player_id")
            ):
                key = (provider_id, str(provider.get("name") or ""), "conflict")
                rejections_by_key[key] = {
                    **identity,
                    "reason": "conflicting_canonical_player",
                    "existing_fpl_player_id": existing.get("fpl_player_id"),
                }
                continue
            identity_by_provider[provider_id] = identity

    identities = sorted(
        identity_by_provider.values(),
        key=lambda row: identity_sort_key(row.get("provider_player_id")),
    )
    rejections = sorted(
        rejections_by_key.values(),
        key=lambda row: (
            identity_sort_key(row.get("provider_player_id")),
            str(row.get("provider_name") or ""),
        ),
    )
    return identities, rejections

def parse_utc(value: Any) -> datetime | None:
    """Parse an API/CSV timestamp and normalise it to UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fixture_team_key(value: Any) -> str:
    """Create a conservative team key for cross-source fixture matching."""
    canonical = mapping.resolve_team_name(str(value or ""))
    text = norm(canonical or value)
    ignored = {"ac", "afc", "cf", "club", "fc", "fk", "sc", "sv"}
    return " ".join(token for token in text.split() if token not in ignored)


def parse_friendly_match_id(match_id: Any) -> tuple[str, str] | None:
    """Recover home/away names from the repository's friendly match ID."""
    found = re.match(
        r"^\d{2}-\d{2}-friendly-(.+?)-vs-(.+)-\d{4}-\d{2}-\d{2}$",
        str(match_id or ""),
    )
    if not found:
        return None
    return found.group(1).replace("-", " "), found.group(2).replace("-", " ")


def fixture_orientation(
    event_home: str,
    event_away: str,
    repo_home: str,
    repo_away: str,
) -> str | None:
    """Return the cross-source orientation when both opponent identities match."""
    if event_home == repo_home and event_away == repo_away:
        return "same"
    if event_home == repo_away and event_away == repo_home:
        return "swapped"
    return None


def mapped_friendly_betting_targets(
    exact_matches: list[dict[str, Any]],
    events: dict[Any, dict],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return chronologically ordered future friendlies with canonical match IDs."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    event_by_id = {id_key(event.get("id")): event for event in events.values()}
    finished_statuses = {"finished", "afterextra", "afterpenalties", "ft"}
    targets: list[dict[str, Any]] = []
    for identity in exact_matches:
        event = event_by_id.get(id_key(identity.get("event_id")))
        kickoff = parse_utc((event or {}).get("event_date"))
        if (
            not event
            or not kickoff
            or kickoff < current
            or str(event.get("status", "")).casefold() in finished_statuses
        ):
            continue
        targets.append(
            {
                "event": event,
                "canonical_match_id": identity.get("repo_match_id"),
                "orientation": identity.get("orientation"),
                "kickoff": kickoff.isoformat(),
            }
        )
    return sorted(targets, key=lambda row: (row["kickoff"], str(row["canonical_match_id"])))

def evaluate_friendlies(
    api: Api,
    friendly_league: dict | None,
    season_label: str,
    sample_size: int,
    out: Path,
) -> dict[str, Any]:
    REPORTER.section(f"6. Club friendlies ({season_label})")
    folder = DATA_ROOT / season_label / "By Tournament" / "Friendlies" / "GW0"
    repo_matches = read_csv(folder / "matches.csv")
    repo_stats = read_csv(folder / "playermatchstats.csv")
    repo_with_stats = {row.get("match_id") for row in repo_stats}
    repo_with_xg = sum(
        number(match.get("home_expected_goals_xg")) is not None
        for match in repo_matches
    )
    REPORTER.say(
        f"Repository: **{len(repo_matches)} friendlies**, "
        f"**{len(repo_with_stats)} with player stats**, **{repo_with_xg} with xG**.\n"
    )

    teams, resolved = resolve_current_teams(api, season_label)
    REPORTER.say(f"Team identity test: **{len(resolved)}/{len(teams)}** resolved.\n")
    start_year = int(season_label.split("-")[0])
    date_from = f"{start_year}-06-15T00:00:00Z"
    date_to = f"{start_year}-08-16T23:59:59Z"
    events: dict[Any, dict] = {}
    for team in resolved.values():
        payload = api.get(
            f"/api/v2/teams/{team['id']}/fixtures/",
            date_from=date_from,
            date_to=date_to,
            limit=100,
        )
        for event in response_rows(payload, "fixtures", "events"):
            name = str(event.get("league_name") or event.get("league") or "")
            league_match = (
                friendly_league
                and id_key(event.get("league_id")) == id_key(friendly_league.get("id"))
            )
            if league_match or re.search(r"club friendl", name, re.I):
                events[event.get("id")] = event

    # Match both opponents and kickoff time. The public CSV often omits the
    # non-repository team code, so recover that opponent from the stable match
    # ID slug. Matching is one-to-one to avoid same-club/same-date inflation.
    repo_team_by_code = {
        id_key(team.get("code")): team.get("name") for team in teams
    }
    repo_candidates: list[dict[str, Any]] = []
    for match in repo_matches:
        parsed_sides = parse_friendly_match_id(match.get("match_id"))
        if not parsed_sides:
            continue
        home_name = (
            repo_team_by_code.get(id_key(match.get("home_team"))) or parsed_sides[0]
        )
        away_name = (
            repo_team_by_code.get(id_key(match.get("away_team"))) or parsed_sides[1]
        )
        repo_candidates.append(
            {
                "match": match,
                "match_id": str(match.get("match_id")),
                "home_key": fixture_team_key(home_name),
                "away_key": fixture_team_key(away_name),
                "kickoff": parse_utc(match.get("kickoff_time")),
            }
        )

    provider_candidates = [
        {
            "event": event,
            "event_id": id_key(event.get("id")),
            "home_key": fixture_team_key(event.get("home_team")),
            "away_key": fixture_team_key(event.get("away_team")),
            "kickoff": parse_utc(event.get("event_date")),
        }
        for event in events.values()
    ]
    possible_matches: list[tuple[float, int, int, str]] = []
    for event_index, event in enumerate(provider_candidates):
        if not event["kickoff"] or not event["home_key"] or not event["away_key"]:
            continue
        for repo_index, candidate in enumerate(repo_candidates):
            orientation = fixture_orientation(
                event["home_key"],
                event["away_key"],
                candidate["home_key"],
                candidate["away_key"],
            )
            if not orientation or not candidate["kickoff"]:
                continue
            delta_minutes = abs(
                (event["kickoff"] - candidate["kickoff"]).total_seconds()
            ) / 60
            if delta_minutes <= 360:
                possible_matches.append(
                    (delta_minutes, event_index, repo_index, orientation)
                )

    used_events: set[int] = set()
    used_repo: set[int] = set()
    exact_matches: list[dict[str, Any]] = []
    for delta_minutes, event_index, repo_index, orientation in sorted(possible_matches):
        if event_index in used_events or repo_index in used_repo:
            continue
        used_events.add(event_index)
        used_repo.add(repo_index)
        event = provider_candidates[event_index]
        candidate = repo_candidates[repo_index]
        exact_matches.append(
            {
                "event_id": event["event_id"],
                "repo_match_id": candidate["match_id"],
                "home": event["event"].get("home_team"),
                "away": event["event"].get("away_team"),
                "api_kickoff": event["event"].get("event_date"),
                "repo_kickoff": candidate["match"].get("kickoff_time"),
                "orientation": orientation,
                "orientation_swapped": orientation == "swapped",
                "kickoff_delta_minutes": round(delta_minutes, 1),
            }
        )

    matched_event_ids = {match["event_id"] for match in exact_matches}
    matched_repo_ids = {match["repo_match_id"] for match in exact_matches}

    # Keep a looser current-club/date signal for diagnosis, but deliberately
    # exclude it from coverage because it does not confirm the opponent.
    current_club_keys = {
        key for key in (fixture_team_key(team.get("name")) for team in teams) if key
    }
    partial_candidates: list[tuple[float, int, int, list[str]]] = []
    for event_index, event in enumerate(provider_candidates):
        if event_index in used_events or not event["kickoff"]:
            continue
        event_clubs = {event["home_key"], event["away_key"]} & current_club_keys
        if not event_clubs:
            continue
        for repo_index, candidate in enumerate(repo_candidates):
            if repo_index in used_repo or not candidate["kickoff"]:
                continue
            repo_clubs = {
                candidate["home_key"], candidate["away_key"]
            } & current_club_keys
            shared = sorted(event_clubs & repo_clubs)
            if shared and event["kickoff"].date() == candidate["kickoff"].date():
                delta = abs(
                    (event["kickoff"] - candidate["kickoff"]).total_seconds()
                ) / 60
                partial_candidates.append((delta, event_index, repo_index, shared))

    partial_event_indexes: set[int] = set()
    partial_repo_indexes: set[int] = set()
    partial_matches: list[dict[str, Any]] = []
    for delta, event_index, repo_index, shared in sorted(partial_candidates):
        if event_index in partial_event_indexes or repo_index in partial_repo_indexes:
            continue
        partial_event_indexes.add(event_index)
        partial_repo_indexes.add(repo_index)
        event = provider_candidates[event_index]
        candidate = repo_candidates[repo_index]
        partial_matches.append(
            {
                "event_id": event["event_id"],
                "repo_match_id": candidate["match_id"],
                "shared_current_clubs": shared,
                "api_fixture": (
                    f"{event['event'].get('home_team')} v "
                    f"{event['event'].get('away_team')}"
                ),
                "repo_fixture_keys": (
                    f"{candidate['home_key']} v {candidate['away_key']}"
                ),
                "kickoff_delta_minutes": round(delta, 1),
                "reason": "same current club/date only; full opponent-and-kickoff rule not met",
            }
        )

    api_only = [
        event for event in events.values()
        if id_key(event.get("id")) not in matched_event_ids
    ]
    repo_only = [
        match for match in repo_matches
        if str(match.get("match_id")) not in matched_repo_ids
    ]

    dump_json(out, "friendly_event_identity_map.json", exact_matches)
    finished_statuses = {"finished", "afterextra", "afterpenalties", "ft"}
    event_by_id = {id_key(event.get("id")): event for event in events.values()}
    completed_events = [
        event
        for event in events.values()
        if str(event.get("status", "")).casefold() in finished_statuses
    ]
    completed_exact_matches = [
        (identity, event_by_id.get(id_key(identity.get("event_id"))))
        for identity in exact_matches
        if event_by_id.get(id_key(identity.get("event_id")))
        and str(
            event_by_id[id_key(identity.get("event_id"))].get("status", "")
        ).casefold()
        in finished_statuses
    ]
    completed_exact_matches.sort(
        key=lambda pair: (
            parse_utc((pair[1] or {}).get("event_date"))
            or datetime.max.replace(tzinfo=timezone.utc),
            identity_sort_key(pair[0].get("event_id")),
        )
    )

    friendly_identities, friendly_identity_rejections = (
        build_friendly_player_identity_map(api, teams, resolved, season_label)
    )
    friendly_identity_lookup = {
        id_key(row.get("provider_player_id")): row for row in friendly_identities
    }
    known_provider_names = {
        id_key(row.get("provider_player_id")): row.get("provider_name")
        for row in [*friendly_identities, *friendly_identity_rejections]
        if row.get("provider_player_id") is not None
    }
    current_provider_team_ids = {
        id_key(team.get("id")) for team in resolved.values() if team.get("id") is not None
    }

    samples: list[dict[str, Any]] = []
    for identity, event in completed_exact_matches[:sample_size]:
        if not event:
            continue
        payload = api.get(f"/api/v2/events/{event['id']}/player-stats/")
        rows = response_rows(payload, "player_stats")
        dump_json(out, f"friendly_player_stats_event_{event['id']}.json", rows)
        event_stats = api.get(f"/api/v2/events/{event['id']}/stats/") or {}
        dump_json(out, f"friendly_event_{event['id']}_stats.json", event_stats)
        for row in rows:
            provider_id = id_key(row.get("player_id"))
            mapped_identity = friendly_identity_lookup.get(provider_id)
            if mapped_identity:
                mapped_identity.setdefault("provider_event_ids", [])
                mapped_identity.setdefault("canonical_match_ids", [])
                if event.get("id") not in mapped_identity["provider_event_ids"]:
                    mapped_identity["provider_event_ids"].append(event.get("id"))
                if identity.get("repo_match_id") not in mapped_identity["canonical_match_ids"]:
                    mapped_identity["canonical_match_ids"].append(
                        identity.get("repo_match_id")
                    )
                continue
            provider_team_id = id_key(row.get("team_id"))
            friendly_identity_rejections.append(
                {
                    "season": season_label,
                    "provider_player_id": row.get("player_id"),
                    "provider_name": known_provider_names.get(provider_id),
                    "provider_team_id": row.get("team_id"),
                    "provider_event_id": event.get("id"),
                    "canonical_match_id": identity.get("repo_match_id"),
                    "reason": (
                        "missing_provider_player_id"
                        if not provider_id
                        else (
                            "unresolved_current_pl_player"
                            if provider_team_id in current_provider_team_ids
                            else "non_pl_opponent_not_in_season_roster"
                        )
                    ),
                }
            )
        samples.append(
            {
                "event_id": event.get("id"),
                "repo_match_id": identity.get("repo_match_id"),
                "orientation": identity.get("orientation"),
                "home": event.get("home_team"),
                "away": event.get("away_team"),
                "date": event.get("event_date"),
                "player_rows": len(rows),
                "event_stats_present": bool(event_stats),
                "shot_rows": len(event_stats.get("shotmap") or []),
            }
        )

    for identity in friendly_identities:
        identity.setdefault("provider_event_ids", [])
        identity.setdefault("canonical_match_ids", [])
        identity["provider_event_ids"] = sorted(
            identity["provider_event_ids"], key=identity_sort_key
        )
        identity["canonical_match_ids"] = sorted(
            identity["canonical_match_ids"], key=lambda value: str(value or "")
        )
    friendly_identity_rejections.sort(
        key=lambda row: (
            identity_sort_key(row.get("provider_player_id")),
            identity_sort_key(row.get("provider_event_id")),
            str(row.get("reason") or ""),
        )
    )
    dump_json(out, "friendly_player_identity_map.json", friendly_identities)
    dump_json(
        out,
        "friendly_player_identity_rejections.json",
        friendly_identity_rejections,
    )
    betting_targets = mapped_friendly_betting_targets(exact_matches, events)
    dump_json(
        out,
        "friendlies_sample.json",
        {
            "events": list(events.values())[:100],
            "completed_events": completed_events[:100],
            "completed_exact_events": len(completed_exact_matches),
            "completed_sample_coverage": samples,
            "matching_rule": {
                "opponents": "normalised opponent pair must match; orientation may swap",
                "max_kickoff_delta_minutes": 360,
                "one_to_one": True,
            },
            "exact_matches": exact_matches,
            "partial_candidates_not_counted_as_matches": partial_matches,
            "mapped_event_ids": sorted(matched_event_ids, key=identity_sort_key),
            "mapped_repo_match_ids": sorted(matched_repo_ids),
            "api_only": api_only,
            "repo_only": repo_only,
            "mapped_upcoming_betting_targets": [
                {
                    "event_id": target["event"].get("id"),
                    "canonical_match_id": target.get("canonical_match_id"),
                    "kickoff": target.get("kickoff"),
                    "orientation": target.get("orientation"),
                }
                for target in betting_targets
            ],
        },
    )
    covered = sum(sample["player_rows"] > 0 for sample in samples)
    REPORTER.say(
        f"API: **{len(events)} scheduled club friendlies** involving current PL teams; "
        f"**{len(completed_events)} completed** at capture time."
    )
    REPORTER.say(
        f"Opponent-aware one-to-one repository overlap (kickoff delta <= 6h): "
        f"**{len(matched_repo_ids)}/{len(repo_matches)} fixtures**; "
        f"API-only: **{len(api_only)}**; repository-only: **{len(repo_only)}**."
    )
    REPORTER.say(
        f"Loose club/date candidates excluded from coverage: "
        f"**{len(partial_matches)}**."
    )
    REPORTER.say(
        f"Exact-mapped completed-match player-stat coverage: **{covered}/{len(samples)}** sampled.\n"
    )
    if samples:
        REPORTER.say("| Event | Fixture | Date | Player rows |")
        REPORTER.say("|---|---|---|---:|")
        for sample in samples:
            REPORTER.say(
                f"| `{sample['event_id']}` | {sample['home']} v {sample['away']} | "
                f"{str(sample['date'])[:10]} | {sample['player_rows']} |"
            )
        REPORTER.say("")

    team_rate = len(resolved) / len(teams) if teams else 0
    REPORTER.check(
        "friendlies.team_mapping",
        "friendlies",
        "pass" if team_rate == 1 else ("warn" if team_rate >= 0.9 else "fail"),
        f"{len(resolved)}/{len(teams)} current PL teams resolved",
        mapped=len(resolved),
        total=len(teams),
        rate=team_rate,
    )
    fixture_rate = len(matched_repo_ids) / len(repo_matches) if repo_matches else 0
    REPORTER.check(
        "friendlies.fixture_coverage",
        "friendlies",
        "pass" if fixture_rate >= 0.9 else "warn",
        f"{len(matched_repo_ids)}/{len(repo_matches)} repository friendlies matched by opponents and kickoff",
        api_events=len(events),
        repo_events=len(repo_matches),
        mapped_repo_events=len(matched_repo_ids),
        match_rate=round(fixture_rate, 4),
        partial_candidates=len(partial_matches),
        api_only=len(api_only),
        repo_only=len(repo_only),
    )
    REPORTER.check(
        "friendlies.player_stats",
        "friendlies",
        "pass" if covered else "warn",
        f"{covered}/{len(samples)} exact-mapped completed friendlies have player stats",
        covered=covered,
        sampled=len(samples),
        completed_events=len(completed_events),
        completed_exact_events=len(completed_exact_matches),
    )
    return {
        "teams": teams,
        "resolved": resolved,
        "events": events,
        "exact_matches": exact_matches,
        "betting_targets": betting_targets,
        "friendly_player_identities": friendly_identities,
    }


def evaluate_lineups_incidents(
    api: Api, event: dict | None, out: Path
) -> None:
    REPORTER.section("7. Lineups, availability and incidents")
    if not event:
        REPORTER.say("No matched event available for enrichment testing.\n")
        for check_id in ("lineups", "incidents"):
            REPORTER.check(
                f"enrichment.{check_id}",
                "enrichment",
                "skip",
                "No matched event available",
            )
        return

    event_id = event["id"]
    lineups = api.get(f"/api/v2/events/{event_id}/lineups/")
    incidents = api.get(f"/api/v2/events/{event_id}/incidents/")
    detail = api.get(f"/api/v2/events/{event_id}/")
    dump_json(out, f"event_{event_id}_lineups.json", lineups or {})
    dump_json(out, f"event_{event_id}_incidents.json", incidents or {})
    dump_json(out, f"event_{event_id}_detail.json", detail or {})

    sides = (lineups or {}).get("lineups") or {}
    lineup_players = 0
    for side in ("home", "away"):
        entry = sides.get(side) or {}
        lineup_players += len(entry.get("players") or [])
        lineup_players += len(entry.get("substitutes") or [])
    unavailable = (lineups or {}).get("unavailable_players") or {}
    unavailable_count = sum(
        len(unavailable.get(side) or []) for side in ("home", "away")
    )
    incident_rows = response_rows(incidents, "incidents")
    incident_types = sorted(
        {str(row.get("type")) for row in incident_rows if row.get("type")}
    )
    REPORTER.say(
        f"Event `{event_id}` lineup status: "
        f"`{(lineups or {}).get('lineup_status')}`; "
        f"**{lineup_players} lineup/substitute rows**, "
        f"**{unavailable_count} unavailable-player rows**."
    )
    REPORTER.say(
        f"Incidents: **{len(incident_rows)}**, types: "
        f"{', '.join(incident_types) or 'none'}."
    )
    REPORTER.say(
        "Bzzoiro availability is evaluated as a separate source and must not "
        "overwrite FPL `status`, `news`, or chance-of-playing fields.\n"
    )
    REPORTER.check(
        "enrichment.lineups",
        "enrichment",
        "pass" if lineup_players >= 22 else ("warn" if lineups else "fail"),
        f"{lineup_players} player rows; status {(lineups or {}).get('lineup_status')}",
        event_id=event_id,
        lineup_status=(lineups or {}).get("lineup_status"),
        player_rows=lineup_players,
        unavailable_rows=unavailable_count,
    )
    REPORTER.check(
        "enrichment.incidents",
        "enrichment",
        "pass" if incident_rows else "warn",
        f"{len(incident_rows)} incidents returned",
        event_id=event_id,
        count=len(incident_rows),
        types=incident_types,
    )


def evaluate_player_profiles(
    api: Api,
    season_label: str,
    resolved_teams: dict[str, dict],
    sample_size: int,
    out: Path,
) -> None:
    REPORTER.section("8. Player profiles and separate availability source")
    fpl_players = read_csv(DATA_ROOT / season_label / "players.csv")
    fpl_stats = {
        id_key(row.get("id")): row
        for row in read_csv(DATA_ROOT / season_label / "playerstats.csv")
    }
    fpl_by_full, fpl_by_surname = _repo_player_indexes(fpl_players)
    provider_players: list[dict] = []
    for team in list(resolved_teams.values())[:3]:
        squad = api.get(f"/api/v2/teams/{team['id']}/squad/")
        provider_players.extend(response_rows(squad, "players"))
        if len(provider_players) >= sample_size:
            break

    samples: list[dict[str, Any]] = []
    for provider in provider_players[:sample_size]:
        detail = api.get(f"/api/v2/players/{provider['id']}/") or {}
        canonical = _match_repo_player(detail or provider, fpl_by_full, fpl_by_surname)
        fpl = fpl_stats.get(id_key((canonical or {}).get("player_id")), {})
        samples.append(
            {
                "provider_player_id": detail.get("id") or provider.get("id"),
                "provider_name": detail.get("name") or provider.get("name"),
                "fpl_player_id": (canonical or {}).get("player_id"),
                "fpl_status": fpl.get("status"),
                "fpl_news": fpl.get("news"),
                "provider_availability": detail.get("availability"),
                "provider_injury_risk": detail.get("injury_risk"),
                "contract_until": detail.get("contract_until"),
                "market_value_eur": detail.get("market_value_eur"),
            }
        )
    dump_json(out, "player_profile_sample.json", samples)
    mapped = sum(sample["fpl_player_id"] is not None for sample in samples)
    availability = sum(
        sample["provider_availability"] is not None for sample in samples
    )
    REPORTER.say(
        f"Sample: **{mapped}/{len(samples)}** profiles mapped to FPL players; "
        f"**{availability}/{len(samples)}** expose provider availability."
    )
    REPORTER.say(
        "The report keeps provider availability alongside FPL values for comparison; "
        "it does not combine them.\n"
    )
    REPORTER.check(
        "enrichment.player_profiles",
        "enrichment",
        "pass" if mapped >= max(1, len(samples) // 2) else "warn",
        f"{mapped}/{len(samples)} profiles mapped; {availability} with availability",
        sampled=len(samples),
        mapped=mapped,
        availability_present=availability,
    )


def choose_betting_target(
    mapped_targets: list[dict[str, Any]],
    best_rows: list[dict],
    fallback_event: dict | None,
) -> dict[str, Any] | None:
    """Prefer a canonically mapped upcoming friendly over unrelated global odds."""
    global_event_ids = {
        id_key(row.get("event_id"))
        for row in best_rows
        if isinstance(row, dict) and row.get("event_id") is not None
    }
    if mapped_targets:
        selected = next(
            (
                target
                for target in mapped_targets
                if id_key((target.get("event") or {}).get("id")) in global_event_ids
            ),
            mapped_targets[0],
        )
        selected_event = selected.get("event") or {}
        return {
            "event": selected_event,
            "canonical_match_id": selected.get("canonical_match_id"),
            "orientation": selected.get("orientation"),
            "selection": (
                "mapped_upcoming_friendly_with_global_odds"
                if id_key(selected_event.get("id")) in global_event_ids
                else "next_mapped_upcoming_friendly"
            ),
        }
    global_event_id = next(
        (
            row.get("event_id")
            for row in best_rows
            if isinstance(row, dict) and row.get("event_id") is not None
        ),
        None,
    )
    if global_event_id is not None:
        return {
            "event": {"id": global_event_id},
            "canonical_match_id": None,
            "orientation": None,
            "selection": "unmapped_global_best_odds_fallback",
        }
    if fallback_event:
        return {
            "event": fallback_event,
            "canonical_match_id": None,
            "orientation": None,
            "selection": "unmapped_historical_event_fallback",
        }
    return None

def evaluate_betting(
    api: Api,
    event: dict | None,
    out: Path,
    mapped_targets: list[dict[str, Any]] | None = None,
) -> None:
    REPORTER.section("9. Betting and prediction data (evaluation only)")
    best = api.get("/api/v2/odds/best/") or {}
    predictions = api.get("/api/v2/predictions/", limit=20) or {}
    best_rows = response_rows(best, "odds")
    prediction_rows = response_rows(predictions, "predictions")
    dump_json(out, "odds_best_sample.json", best_rows[:20])
    dump_json(out, "predictions_sample.json", prediction_rows[:20])

    selection = choose_betting_target(mapped_targets or [], best_rows, event)
    selected_event = (selection or {}).get("event") or {}
    betting_event_id = selected_event.get("id")
    dump_json(
        out,
        "mapped_betting_event.json",
        {
            "provider_event_id": betting_event_id,
            "canonical_match_id": (selection or {}).get("canonical_match_id"),
            "selection": (selection or {}).get("selection"),
            "orientation": (selection or {}).get("orientation"),
            "is_canonical_mapped": bool(
                selection and selection.get("canonical_match_id") is not None
            ),
            "home": selected_event.get("home_team"),
            "away": selected_event.get("away_team"),
            "kickoff": selected_event.get("event_date"),
        },
    )

    event_payloads: dict[str, Any] = {}
    if betting_event_id is not None:
        event_id = betting_event_id
        routes = {
            "odds": f"/api/v2/events/{event_id}/odds/",
            "odds_comparison": f"/api/v2/events/{event_id}/odds/comparison/",
            "prediction": f"/api/v2/events/{event_id}/prediction/",
            "polymarket": f"/api/v2/events/{event_id}/polymarket/",
        }
        for name, route in routes.items():
            event_payloads[name] = api.get(route)
        dump_json(out, f"event_{event_id}_betting.json", event_payloads)

    market_names = sorted(
        {
            str(row.get("market"))
            for row in best_rows
            if isinstance(row, dict) and row.get("market")
        }
    )
    event_odds = (event_payloads.get("odds") or {}).get("odds") or {}
    comparison = event_payloads.get("odds_comparison") or {}
    mapped_prediction = event_payloads.get("prediction")
    polymarket = event_payloads.get("polymarket") or {}
    REPORTER.say(
        f"Global best-odds rows: **{len(best_rows)}**; "
        f"prediction rows sampled: **{len(prediction_rows)}**."
    )
    REPORTER.say(
        f"Event odds fields: **{len(event_odds)}**; bookmaker comparison count: "
        f"**{comparison.get('bookmakers_count', 0)}**; "
        f"Polymarket markets present: **{bool(polymarket.get('markets'))}**."
    )
    REPORTER.say(
        "These are tested as time-sensitive model/market data. They are not "
        "treated as match facts and nothing is imported.\n"
    )
    REPORTER.check(
        "betting.global_odds",
        "betting",
        "pass" if best_rows else "warn",
        f"{len(best_rows)} best-odds rows; markets {', '.join(market_names[:8]) or 'none'}",
        rows=len(best_rows),
        markets=market_names,
    )
    REPORTER.check(
        "betting.predictions",
        "betting",
        "pass" if prediction_rows else "warn",
        f"{len(prediction_rows)} prediction rows sampled",
        rows=len(prediction_rows),
    )
    if selection and selection.get("canonical_match_id") is not None:
        REPORTER.check(
            "betting.mapped_prediction",
            "betting",
            "pass" if mapped_prediction else "warn",
            (
                "Mapped fixture prediction available"
                if mapped_prediction
                else "Mapped fixture has no prediction; global rows are capability evidence only"
            ),
            canonical_match_id=selection.get("canonical_match_id"),
            available=bool(mapped_prediction),
        )
    else:
        REPORTER.check(
            "betting.mapped_prediction",
            "betting",
            "skip",
            "No canonically mapped betting fixture selected",
        )
    REPORTER.check(
        "betting.event_markets",
        "betting",
        "pass" if event_odds else "warn",
        f"{len(event_odds)} event odds fields; "
        f"{comparison.get('bookmakers_count', 0)} bookmakers",
        odds_fields=sorted(event_odds),
        bookmakers_count=comparison.get("bookmakers_count", 0),
        polymarket=bool(polymarket.get("markets")),
    )


def report_manual_gates() -> None:
    REPORTER.section("10. Adoption gates not settled by an API probe")
    REPORTER.say(
        "- **Licensing/redistribution:** confirm that derived Opta/Sportradar-like "
        "data may legally be republished in this open repository."
    )
    REPORTER.say(
        "- **ID stability:** rerun across time and compare provider IDs before "
        "building durable identity bridges."
    )
    REPORTER.say(
        "- **Rate limits and change semantics:** the supplied schema does not "
        "document quotas, `429`, ETags, or stable update timestamps for all routes."
    )
    REPORTER.say(
        "- **Production decision:** this branch evaluates evidence only; it does "
        "not modify canonical data or Supabase.\n"
    )
    REPORTER.check(
        "adoption.licensing",
        "adoption",
        "warn",
        "Manual redistribution/licensing review remains required",
    )


def report_reliability(api: Api) -> None:
    REPORTER.section("11. API reliability for this run")
    summary = api.telemetry_summary()
    REPORTER.say(
        f"Logical requests: **{summary['logical_requests']}**; successful: "
        f"**{summary['successful_requests']}**; HTTP attempts: "
        f"**{summary['http_attempts']}**."
    )
    REPORTER.say(
        f"Median latency: **{summary['median_ms']} ms**; "
        f"p95: **{summary['p95_ms']} ms**; errors: **{len(summary['errors'])}**.\n"
    )
    rate = summary["success_rate"]
    REPORTER.check(
        "reliability.requests",
        "reliability",
        "pass" if rate >= 0.98 else ("warn" if rate >= 0.9 else "fail"),
        f"{summary['successful_requests']}/{summary['logical_requests']} requests succeeded",
        **summary,
    )



def scrub_secret_leaks(out: Path, key: str) -> list[str]:
    """Scrub credential-bearing files; treat unreadable artifacts as unsafe."""
    if not key:
        return []
    unsafe: list[str] = []
    for path in out.rglob("*"):
        if not path.is_file():
            continue
        relative = str(path.relative_to(out))
        try:
            contains_key = key in path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            unsafe.append(f"{relative} [unreadable]")
            continue
        if contains_key:
            unsafe.append(relative)
            path.write_text(
                "Unsafe artifact scrubbed because it contained the API credential.\n",
                encoding="utf-8",
            )
    return unsafe


def _git_output(*args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def repository_name() -> str:
    """Derive the repository slug without depending on the local folder name."""
    remote = _git_output("config", "--get", "remote.origin.url")
    if remote:
        slug = re.split(r"[/\\:]", remote.rstrip("/\\"))[-1]
        if slug.endswith(".git"):
            slug = slug[:-4]
        if slug:
            return slug
    return REPO_ROOT.name


def build_manifest(
    args: argparse.Namespace, api: Api, working_tree_dirty_at_start: bool | None
) -> dict[str, Any]:
    schema_hash = None
    if args.schema and Path(args.schema).expanduser().exists():
        schema_hash = hashlib.sha256(
            Path(args.schema).expanduser().read_bytes()
        ).hexdigest()
    status_after = _git_output("status", "--porcelain")
    execution_files = {}
    for name in ("evaluator.py", "mapping.py", "probe.py"):
        path = Path(__file__).resolve().parent / name
        if path.is_file():
            execution_files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "probe_version": "2.0",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "repository": repository_name(),
        "git_base_commit": _git_output("rev-parse", "HEAD"),
        "git_branch": _git_output("branch", "--show-current"),
        "working_tree_dirty_at_start": working_tree_dirty_at_start,
        "working_tree_dirty_after_capture": (
            bool(status_after) if status_after is not None else None
        ),
        "execution_file_sha256": execution_files,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "api_base_url": api.base,
        "schema_sha256": schema_hash,
        "inputs": {
            "comparison_season": args.comparison_season,
            "friendlies_season": args.friendlies_season,
            "gameweek": args.gameweek,
            "sample_matches": args.sample_matches,
            "friendly_sample": args.friendly_sample,
            "profile_sample": args.profile_sample,
            "identity_detail_limit": args.identity_detail_limit,
            "skip_last_season": args.skip_last_season,
            "skip_friendlies": args.skip_friendlies,
            "skip_enrichment": args.skip_enrichment,
            "skip_betting": args.skip_betting,
            "strict": args.strict,
        },
        "api_summary": api.telemetry_summary(),
    }


def write_artifact_readme(out: Path) -> None:
    text = """# Bzzoiro evaluation snapshot

This directory contains bounded, read-only evaluation evidence generated by
`scripts/bzzoiro/evaluator.py`. It is not canonical FPL Core Insights data. The
normal exporter does not consume it; `integration_preview.py` uses it only
to build the isolated branch review under `scripts/bzzoiro/review_export/`.

- `REPORT.md` is the human-readable result.
- `SUMMARY.json` contains structured pass/warn/fail checks.
- `RUN_MANIFEST.json` records the base commit, start/end tree state, executing-file hashes, inputs, runtime and schema hash.
- `ENDPOINT_TELEMETRY.json` records endpoint status and latency without the API key.
- Other JSON files contain bounded responses plus accepted/rejected identity evidence used by the offline integration preview.

FPL availability and Bzzoiro availability remain separate in the samples.
Betting data is time-sensitive evaluation material, not an observed match fact
or a recommendation. Redistribution/licensing must be resolved before any API
data is incorporated into the public canonical dataset.
"""
    (out / "README.md").write_text(text, encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="bzzoiro_probe_out")
    parser.add_argument("--schema", help="optional local OpenAPI JSON file")
    parser.add_argument("--comparison-season", default="2025-2026")
    parser.add_argument("--friendlies-season", default="2026-2027")
    parser.add_argument("--gameweek", type=int, default=10)
    parser.add_argument("--sample-matches", type=int, default=5)
    parser.add_argument("--friendly-sample", type=int, default=12)
    parser.add_argument("--profile-sample", type=int, default=10)
    parser.add_argument("--identity-detail-limit", type=int, default=50)
    parser.add_argument("--skip-last-season", action="store_true")
    parser.add_argument("--skip-friendlies", action="store_true")
    parser.add_argument("--skip-enrichment", action="store_true")
    parser.add_argument("--skip-betting", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when any evaluation check fails",
    )
    args = parser.parse_args(argv)
    args.key = os.environ.get("BZZOIRO_API_KEY")
    return args


def main(argv: list[str] | None = None) -> int:
    global REPORTER
    REPORTER = Reporter()
    args = parse_args(argv)
    status_at_start = _git_output("status", "--porcelain")
    working_tree_dirty_at_start = (
        bool(status_at_start) if status_at_start is not None else None
    )
    if not args.key:
        print("No API key. Set BZZOIRO_API_KEY in the environment.", file=sys.stderr)
        return 2
    if args.gameweek < 1 or args.gameweek > 38:
        print("--gameweek must be between 1 and 38.", file=sys.stderr)
        return 2
    for name in (
        "sample_matches", "friendly_sample", "profile_sample", "identity_detail_limit"
    ):
        if getattr(args, name) < 1:
            print(f"--{name.replace('_', '-')} must be positive.", file=sys.stderr)
            return 2
    try:
        out = safe_output_path(args.out)
        ensure_owned_output_directory(out)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    clear_owned_artifacts(out)
    api = Api(args.key)

    evaluate_schema(args.schema, out)
    competitions = evaluate_competitions(api, out)
    premier_league = competitions.get("Premier League")
    seasons: dict[str, dict] = {}
    if premier_league:
        seasons = evaluate_seasons(api, premier_league, out)
    else:
        REPORTER.check(
            "coverage.seasons",
            "coverage",
            "skip",
            "Premier League was not resolved",
        )

    context: dict[str, Any] = {}
    comparison = seasons.get(args.comparison_season)
    if not args.skip_last_season and premier_league and comparison:
        context = evaluate_overlap(
            api,
            premier_league,
            comparison,
            args.comparison_season,
            args.gameweek,
            args.sample_matches,
            args.identity_detail_limit,
            out,
        )
    else:
        REPORTER.check(
            "overlap.cross_check",
            "overlap",
            "skip",
            "Historical cross-check disabled or season unavailable",
        )

    event = context.get("sample_event")
    if not args.skip_enrichment:
        evaluate_shots(api, event, out)

    friendly_context: dict[str, Any] = {}
    if not args.skip_friendlies:
        friendly_context = evaluate_friendlies(
            api,
            competitions.get("Club Friendlies"),
            args.friendlies_season,
            args.friendly_sample,
            out,
        )

    if not args.skip_enrichment:
        evaluate_lineups_incidents(api, event, out)
        resolved = friendly_context.get("resolved", {})
        if not resolved:
            _, resolved = resolve_current_teams(api, args.friendlies_season)
        evaluate_player_profiles(
            api,
            args.friendlies_season,
            resolved,
            args.profile_sample,
            out,
        )

    if not args.skip_betting:
        evaluate_betting(
            api,
            event,
            out,
            mapped_targets=friendly_context.get("betting_targets", []),
        )

    report_manual_gates()
    report_reliability(api)
    dump_json(out, "ENDPOINT_TELEMETRY.json", api.telemetry)
    dump_json(
        out,
        "RUN_MANIFEST.json",
        build_manifest(args, api, working_tree_dirty_at_start),
    )
    write_artifact_readme(out)

    report = REPORTER.render(api, args)
    (out / "REPORT.md").write_text(report, encoding="utf-8")
    dump_json(out, "SUMMARY.json", REPORTER.summary(api, args))

    unsafe_files = scrub_secret_leaks(out, args.key)
    if unsafe_files:
        (out / "SECRET_SCAN_OK").unlink(missing_ok=True)
        (out / "SECRET_SCAN_FAILED.txt").write_text(
            "Credential-bearing artifacts were scrubbed and must not be published.\n",
            encoding="utf-8",
        )
        print(
            "API key was detected and scrubbed from probe output; publication blocked.",
            file=sys.stderr,
        )
        return 3
    (out / "SECRET_SCAN_OK").write_text(
        "Artifact credential scan passed.\n", encoding="utf-8"
    )
    if api.ok_requests == 0:
        print("No API request succeeded.", file=sys.stderr)
        return 2
    failures = [check for check in REPORTER.checks if check.status == "fail"]
    print(f"Report written to {out / 'REPORT.md'}", file=sys.stderr)
    if args.strict and failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
