"""Build a deterministic, offline Bzzoiro -> FPL pipeline review export.

This module deliberately does not write to the repository's canonical
``data/`` tree or to a live Supabase project.  It stages captured API samples
in an in-memory SQLite database (including the private source identities),
then emits only canonical-ID public review files under ``review_export``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
SAMPLE_DIR = SCRIPT_DIR / "sample_data"
DEFAULT_OUTPUT = SCRIPT_DIR / "review_export"

SOURCE_NAME = "bzzoiro"
HISTORICAL_SEASON = "2025-2026"
CURRENT_SEASON = "2026-2027"

PMS_DIRECT_SAFE = {
    "minutes_played": "minutes_played",
    "goals": "goals",
    "goal_assist": "assists",
    "total_shots": "total_shots",
    "shots_on_target": "shots_on_target",
    "expected_goals": "xg",
    "expected_assists": "xa",
    "won_contest": "successful_dribbles",
    "touches": "touches",
    "accurate_pass": "accurate_passes",
    "key_pass": "chances_created",
    "accurate_cross": "accurate_crosses",
    "accurate_long_balls": "accurate_long_balls",
    "won_tackle": "tackles_won",
    "total_tackle": "tackles",
    "interception": "interceptions",
    "ball_recovery": "recoveries",
    "total_clearance": "clearances",
    "was_fouled": "was_fouled",
    "fouls": "fouls_committed",
    "saves": "saves",
    "goals_conceded": "goals_conceded",
    "dispossessed": "dispossessed",
}

PLAYER_ENRICHMENT_COLUMNS = [
    "player_id",
    "match_id",
    "player_name",
    "rating",
    "possession_lost",
    "attacking_shots_blocked",
    "total_passes",
    "total_long_balls",
    "total_crosses",
    "total_dribbles",
    "ground_duels_lost",
    "aerial_duels_lost",
    "yellow_cards",
    "red_cards",
    "goalkeeper_punches",
    "penalties_won",
    "penalties_conceded",
    "penalties_faced",
    "penalties_saved",
]

MATCH_ENRICHMENT_COLUMNS = [
    "match_id",
    "travel_distance_km",
    "weather_description",
    "temperature_c",
    "wind_speed",
    "pitch_condition",
    "is_local_derby",
    "is_neutral_ground",
    "attendance",
    "lineup_status",
    "lineup_confidence",
]

SHOT_COLUMNS = [
    "match_id",
    "shot_index",
    "minute",
    "added_time",
    "is_home",
    "player_id",
    "player_name",
    "outcome",
    "situation",
    "body_part",
    "xg",
    "xgot",
    "start_x",
    "start_y",
    "goal_mouth_y",
    "goal_mouth_z",
    "goal_mouth_location",
]

LINEUP_COLUMNS = [
    "match_id",
    "team_side",
    "team_code",
    "player_id",
    "player_name",
    "position",
    "jersey_number",
    "is_starting",
    "formation",
    "lineup_status",
    "confidence",
]

INCIDENT_COLUMNS = [
    "match_id",
    "incident_index",
    "incident_type",
    "minute",
    "added_time",
    "team_side",
    "player_id",
    "player_name",
    "secondary_player_id",
    "secondary_player_name",
    "assist_player_id",
    "assist_player_name",
    "card_type",
    "goal_type",
    "home_score",
    "away_score",
    "text",
]

MOMENTUM_COLUMNS = ["match_id", "minute", "value"]
XG_MINUTE_COLUMNS = [
    "match_id",
    "minute",
    "home_xg",
    "away_xg",
    "home_cumulative_xg",
    "away_cumulative_xg",
]
AVERAGE_POSITION_COLUMNS = [
    "match_id",
    "team_side",
    "player_id",
    "player_name",
    "jersey_number",
    "position",
    "x",
    "y",
]

AVAILABILITY_COLUMNS = [
    "season",
    "player_id",
    "player_name",
    "bzzoiro_availability",
    "bzzoiro_injury_risk",
    "contract_until",
    "market_value_eur",
]

COMPETITION_COLUMNS = [
    "competition_name",
    "country",
    "is_women",
    "is_active",
    "current_season_name",
    "current_season_year",
    "season_start",
    "season_end",
]

ODDS_COLUMNS = [
    "match_id",
    "kickoff_time",
    "competition",
    "home_team",
    "away_team",
    "market",
    "outcome",
    "line",
    "outcome_name",
    "decimal_odds",
    "bookmaker",
    "odds_updated_at",
]

PREDICTION_COLUMNS = [
    "match_id",
    "kickoff_time",
    "competition",
    "home_team",
    "away_team",
    "prediction_created_at",
    "model_version",
    "model_confidence",
    "predicted_result",
    "prob_home",
    "prob_draw",
    "prob_away",
    "prob_over_15",
    "prob_over_25",
    "prob_over_35",
    "prob_btts_yes",
    "recommended_favorite",
    "recommend_over_15",
    "recommend_over_25",
    "recommend_over_35",
    "recommend_btts",
]

IDENTITY_AUDIT_COLUMNS = [
    "entity",
    "season",
    "canonical_id",
    "canonical_name",
    "resolution_method",
    "confidence",
]
REJECTION_SUMMARY_COLUMNS = ["entity", "season", "reason", "row_count"]
MERGE_AUDIT_COLUMNS = [
    "season",
    "target",
    "canonical_rows",
    "accepted_provider_rows",
    "rows_added",
    "blank_cells_filled",
    "supplemental_rows",
]
SCHEMA_AUDIT_COLUMNS = [
    "file",
    "row_count",
    "column_count",
    "matches_canonical_header",
    "fixtures_byte_identical",
]

PROHIBITED_PUBLIC_COLUMNS = {
    "data_source",
    "source_event_id",
    "source_player_id",
    "retrieved_at",
}


@dataclass(frozen=True)
class Context:
    season: str
    label: str
    source_dir: Path
    export_rel: Path


CONTEXTS = (
    Context(
        HISTORICAL_SEASON,
        "Premier League GW10",
        REPO_ROOT
        / "data"
        / HISTORICAL_SEASON
        / "By Tournament"
        / "Premier League"
        / "GW10",
        Path("data")
        / HISTORICAL_SEASON
        / "By Tournament"
        / "Premier League"
        / "GW10",
    ),
    Context(
        CURRENT_SEASON,
        "Friendlies GW0",
        REPO_ROOT
        / "data"
        / CURRENT_SEASON
        / "By Tournament"
        / "Friendlies"
        / "GW0",
        Path("data")
        / CURRENT_SEASON
        / "By Tournament"
        / "Friendlies"
        / "GW0",
    ),
)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> int:
    columns = list(columns)
    prohibited = PROHIBITED_PUBLIC_COLUMNS.intersection(columns)
    if prohibited:
        raise ValueError(f"public export contains prohibited columns: {sorted(prohibited)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in columns})
            count += 1
    return count


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        return f"{value:.12g}"
    return value


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _first_present(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _canonical_sides(home: Any, away: Any, swapped: bool) -> tuple[Any, Any]:
    return (away, home) if swapped else (home, away)


def _canonical_outcome(value: Any, swapped: bool) -> Any:
    if not swapped or value is None:
        return value
    label = str(value)
    replacements = {
        "H": "A",
        "A": "H",
        "HOME": "AWAY",
        "AWAY": "HOME",
    }
    return replacements.get(label.upper(), value)


def _normalise_name(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\b(fc|afc|cf|sc|ac|club)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _canonical_id(value: Any) -> str:
    integer = _safe_int(value)
    return str(integer) if integer is not None else str(value or "").strip()


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nested(data: Mapping[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _set_if_blank(row: dict[str, Any], column: str, value: Any) -> bool:
    if column not in row or _is_blank(value) or not _is_blank(row.get(column)):
        return False
    row[column] = _csv_value(value)
    return True


def _create_database() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(
        """
        CREATE TABLE canonical_match (
            season TEXT NOT NULL,
            match_id TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            PRIMARY KEY (season, match_id)
        );
        CREATE TABLE canonical_player (
            season TEXT NOT NULL,
            player_id TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            team_code TEXT,
            PRIMARY KEY (season, player_id)
        );
        CREATE TABLE source_event_identity (
            data_source TEXT NOT NULL,
            source_event_id INTEGER NOT NULL,
            season TEXT NOT NULL,
            match_id TEXT NOT NULL,
            orientation_swapped INTEGER NOT NULL CHECK (orientation_swapped IN (0, 1)),
            resolution_method TEXT NOT NULL,
            confidence TEXT NOT NULL,
            PRIMARY KEY (data_source, source_event_id),
            UNIQUE (season, match_id),
            FOREIGN KEY (season, match_id)
                REFERENCES canonical_match (season, match_id)
        );
        CREATE TABLE source_player_identity (
            data_source TEXT NOT NULL,
            source_player_id INTEGER NOT NULL,
            season TEXT NOT NULL,
            player_id TEXT NOT NULL,
            resolution_method TEXT NOT NULL,
            confidence TEXT NOT NULL,
            PRIMARY KEY (data_source, source_player_id, season),
            UNIQUE (season, player_id),
            FOREIGN KEY (season, player_id)
                REFERENCES canonical_player (season, player_id)
        );
        CREATE TABLE staged_event (
            data_source TEXT NOT NULL,
            source_event_id INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (data_source, source_event_id),
            FOREIGN KEY (data_source, source_event_id)
                REFERENCES source_event_identity (data_source, source_event_id)
        );
        CREATE TABLE staged_player_match_stat (
            data_source TEXT NOT NULL,
            source_event_id INTEGER NOT NULL,
            source_player_id INTEGER NOT NULL,
            season TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (data_source, source_event_id, source_player_id),
            FOREIGN KEY (data_source, source_event_id)
                REFERENCES source_event_identity (data_source, source_event_id),
            FOREIGN KEY (data_source, source_player_id, season)
                REFERENCES source_player_identity (data_source, source_player_id, season)
        );
        CREATE TABLE rejected_row (
            entity TEXT NOT NULL,
            season TEXT NOT NULL,
            reason TEXT NOT NULL,
            canonical_id TEXT
        );
        """
    )
    return db


def _team_indexes(season: str) -> tuple[dict[str, str], dict[str, str]]:
    _, rows = _read_csv(REPO_ROOT / "data" / season / "teams.csv")
    name_to_code: dict[str, str] = {}
    code_to_name: dict[str, str] = {}
    for row in rows:
        code = _canonical_id(row.get("code"))
        name = row.get("name", "")
        code_to_name[code] = name
        for candidate in (name, row.get("short_name"), row.get("fotmob_name")):
            key = _normalise_name(candidate)
            if key:
                name_to_code[key] = code
    aliases = {
        "brighton and hove albion": "Brighton",
        "brighton hove albion": "Brighton",
        "tottenham hotspur": "Spurs",
        "manchester city": "Man City",
        "manchester united": "Man Utd",
        "newcastle united": "Newcastle",
        "nottingham forest": "Nott'm Forest",
        "wolverhampton wanderers": "Wolves",
        "west ham united": "West Ham",
        "leeds united": "Leeds",
        "liverpool": "Liverpool",
        "afc bournemouth": "Bournemouth",
        "crystal palace": "Crystal Palace",
        "coventry": "Coventry City",
        "hull": "Hull City",
    }
    for alias, canonical_name in aliases.items():
        code = name_to_code.get(_normalise_name(canonical_name))
        if code:
            name_to_code[_normalise_name(alias)] = code
    return name_to_code, code_to_name


def _player_indexes(
    season: str,
) -> tuple[dict[str, dict[str, str]], dict[tuple[str, str], list[str]]]:
    _, rows = _read_csv(REPO_ROOT / "data" / season / "players.csv")
    by_id: dict[str, dict[str, str]] = {}
    by_team_name: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        player_id = _canonical_id(row.get("player_id"))
        if not player_id:
            continue
        by_id[player_id] = row
        team_code = _canonical_id(row.get("team_code"))
        names = {
            _normalise_name(row.get("web_name")),
            _normalise_name(f"{row.get('first_name', '')} {row.get('second_name', '')}"),
            _normalise_name(row.get("second_name")),
        }
        for name in names:
            if name:
                by_team_name.setdefault((team_code, name), []).append(player_id)
    return by_id, by_team_name


def _canonical_fixture_name(
    row: Mapping[str, Any], code_to_name: Mapping[str, str]
) -> str:
    home = code_to_name.get(_canonical_id(row.get("home_team")), "")
    away = code_to_name.get(_canonical_id(row.get("away_team")), "")
    if not home or not away:
        match_id = str(row.get("match_id", ""))
        return match_id
    return f"{home} v {away}"


class PreviewBuilder:
    def __init__(self, output_root: Path):
        self.output_root = output_root
        self.db = _create_database()
        self.summary = _load_json(SAMPLE_DIR / "SUMMARY.json", {})
        self.generated_from = str(self.summary.get("generated_at") or "captured-sample")
        self.context_rows: dict[str, dict[str, Any]] = {}
        self.team_name_to_code: dict[str, dict[str, str]] = {}
        self.team_code_to_name: dict[str, dict[str, str]] = {}
        self.players_by_id: dict[str, dict[str, dict[str, str]]] = {}
        self.players_by_team_name: dict[
            str, dict[tuple[str, str], list[str]]
        ] = {}
        self.event_payloads: dict[int, dict[str, Any]] = {}
        self.event_stats: dict[int, dict[str, Any]] = {}
        self.event_lineups: dict[int, dict[str, Any]] = {}
        self.event_incidents: dict[int, dict[str, Any]] = {}
        self.stage_stats: list[dict[str, Any]] = []
        self.merge_metrics: list[dict[str, Any]] = []
        self.row_counts: dict[str, int] = {}

    def build(self) -> None:
        self._prepare_output()
        self._load_canonical()
        self._load_event_identities()
        self._load_player_identities()
        self._load_event_artifacts()
        self._stage_player_stats()
        self._write_context_exports()
        self._write_supplemental_exports()
        self._write_audits()
        self._write_diff_summary()
        self._write_readme()
        self._write_manifest()
        self.db.close()

    def _prepare_output(self) -> None:
        resolved = self.output_root.resolve()
        default_output = DEFAULT_OUTPUT.resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if resolved == Path(resolved.anchor) or len(resolved.parts) < 3:
            raise ValueError(f"refusing to replace broad output path: {resolved}")
        if resolved != default_output and temporary_root not in resolved.parents:
            raise ValueError(
                "review output must be the checked-in review_export or a temporary directory: "
                f"{resolved}"
            )
        if self.output_root.exists():
            if not self.output_root.is_dir():
                raise ValueError(f"review output is not a directory: {resolved}")
            existing = list(self.output_root.iterdir())
            marker = _load_json(self.output_root / "MANIFEST.json", {})
            if existing and not (
                isinstance(marker, Mapping)
                and marker.get("simulation") is True
                and marker.get("schema_version") == 1
            ):
                raise ValueError(
                    f"refusing to replace an unowned review directory: {resolved}"
                )
            shutil.rmtree(self.output_root)
        self.output_root.mkdir(parents=True)

    def _load_canonical(self) -> None:
        for context in CONTEXTS:
            match_columns, matches = _read_csv(context.source_dir / "matches.csv")
            pms_columns, pms = _read_csv(context.source_dir / "playermatchstats.csv")
            if not match_columns or not pms_columns:
                raise ValueError(f"missing canonical headers for {context.label}")
            self.team_name_to_code[context.season], self.team_code_to_name[
                context.season
            ] = _team_indexes(context.season)
            self.players_by_id[context.season], self.players_by_team_name[
                context.season
            ] = _player_indexes(context.season)
            self.context_rows[context.season] = {
                "context": context,
                "match_columns": match_columns,
                "matches": matches,
                "pms_columns": pms_columns,
                "pms": pms,
            }
            code_names = self.team_code_to_name[context.season]
            for row in matches:
                match_id = str(row["match_id"])
                self.db.execute(
                    "INSERT INTO canonical_match VALUES (?, ?, ?)",
                    (
                        context.season,
                        match_id,
                        _canonical_fixture_name(row, code_names),
                    ),
                )
            for player_id, row in self.players_by_id[context.season].items():
                self.db.execute(
                    "INSERT INTO canonical_player VALUES (?, ?, ?, ?)",
                    (
                        context.season,
                        player_id,
                        row.get("web_name") or row.get("second_name") or player_id,
                        _canonical_id(row.get("team_code")),
                    ),
                )
        self.db.commit()

    def _insert_event_identity(
        self,
        event_id: Any,
        season: str,
        match_id: str,
        orientation_swapped: bool,
        method: str,
        confidence: str,
        payload: Mapping[str, Any] | None,
    ) -> bool:
        source_id = _safe_int(event_id)
        if source_id is None:
            return False
        try:
            self.db.execute(
                """
                INSERT INTO source_event_identity
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    SOURCE_NAME,
                    source_id,
                    season,
                    match_id,
                    int(orientation_swapped),
                    method,
                    confidence,
                ),
            )
        except sqlite3.IntegrityError:
            existing = self.db.execute(
                """
                SELECT season, match_id FROM source_event_identity
                WHERE data_source = ? AND source_event_id = ?
                """,
                (SOURCE_NAME, source_id),
            ).fetchone()
            if existing != (season, match_id):
                self._reject("event_identity", season, "conflicting_identity", match_id)
            return False
        if payload:
            event = dict(payload)
            self.event_payloads[source_id] = event
            self.db.execute(
                "INSERT INTO staged_event VALUES (?, ?, ?)",
                (SOURCE_NAME, source_id, _json_text(event)),
            )
        return True

    def _load_event_identities(self) -> None:
        explicit = self._identity_rows(
            _load_json(SAMPLE_DIR / "event_identity_map.json", [])
        )
        for mapping in sorted(
            explicit,
            key=lambda row: _safe_int(
                row.get("provider_event_id")
                or row.get("source_event_id")
                or row.get("bzzoiro_event_id")
            )
            or -1,
        ):
            source_id = (
                mapping.get("provider_event_id")
                or mapping.get("source_event_id")
                or mapping.get("bzzoiro_event_id")
            )
            match_id = str(
                mapping.get("fpl_match_id")
                or mapping.get("canonical_match_id")
                or ""
            )
            if not match_id:
                continue
            canonical_home = _canonical_id(mapping.get("fpl_home_team_code"))
            canonical_away = _canonical_id(mapping.get("fpl_away_team_code"))
            provider_home = _canonical_id(mapping.get("provider_home_fpl_team_code"))
            provider_away = _canonical_id(mapping.get("provider_away_fpl_team_code"))
            swapped = bool(mapping.get("orientation_swapped"))
            if provider_home and provider_away:
                swapped = (
                    provider_home == canonical_away
                    and provider_away == canonical_home
                )
            event_payload = {
                "id": _safe_int(source_id),
                "event_date": mapping.get("provider_kickoff"),
            }
            self._insert_event_identity(
                source_id,
                str(mapping.get("season") or HISTORICAL_SEASON),
                match_id,
                swapped,
                str(mapping.get("match_method") or "evaluator_exact_identity"),
                str(mapping.get("confidence") or "high"),
                event_payload,
            )

        comparison = _load_json(SAMPLE_DIR / "comparison_events.json", [])
        historical = self.context_rows[HISTORICAL_SEASON]
        historical_matches = historical["matches"]
        name_to_code = self.team_name_to_code[HISTORICAL_SEASON]
        used_matches: set[str] = set()
        for event in sorted(
            (row for row in comparison if isinstance(row, Mapping)),
            key=lambda row: _safe_int(row.get("id")) or -1,
        ):
            event_id = _safe_int(event.get("id"))
            home_code = name_to_code.get(_normalise_name(event.get("home_team")))
            away_code = name_to_code.get(_normalise_name(event.get("away_team")))
            event_dt = _parse_datetime(event.get("event_date"))
            if not home_code or not away_code or event_dt is None:
                continue
            candidates: list[tuple[float, bool, dict[str, Any]]] = []
            for match in historical_matches:
                match_id = str(match.get("match_id"))
                if match_id in used_matches:
                    continue
                match_dt = _parse_datetime(match.get("kickoff_time"))
                if match_dt is None:
                    continue
                same = (
                    _canonical_id(match.get("home_team")) == home_code
                    and _canonical_id(match.get("away_team")) == away_code
                )
                swapped = (
                    _canonical_id(match.get("home_team")) == away_code
                    and _canonical_id(match.get("away_team")) == home_code
                )
                if not same and not swapped:
                    continue
                delta = abs((event_dt - match_dt).total_seconds()) / 60
                if delta <= 360:
                    candidates.append((delta, swapped, match))
            if len(candidates) == 1:
                _, swapped, match = candidates[0]
                match_id = str(match["match_id"])
                if self._insert_event_identity(
                    event_id,
                    HISTORICAL_SEASON,
                    match_id,
                    swapped,
                    "exact_opponents_and_kickoff",
                    "high",
                    event,
                ):
                    used_matches.add(match_id)

        friendly = _load_json(SAMPLE_DIR / "friendlies_sample.json", {})
        friendly_events = {
            _safe_int(row.get("id")): row
            for row in friendly.get("events", [])
            if isinstance(row, Mapping) and _safe_int(row.get("id")) is not None
        }
        friendly_match_ids = {
            str(row["match_id"])
            for row in self.context_rows[CURRENT_SEASON]["matches"]
        }
        accepted_ids: set[int] = set()
        for mapping in sorted(
            friendly.get("exact_matches", []),
            key=lambda row: (str(row.get("repo_match_id")), str(row.get("event_id"))),
        ):
            source_id = _safe_int(mapping.get("event_id"))
            match_id = str(mapping.get("repo_match_id") or "")
            if source_id is None or match_id not in friendly_match_ids:
                self._reject("event_identity", CURRENT_SEASON, "invalid_exact_mapping", match_id)
                continue
            payload = friendly_events.get(source_id)
            if self._insert_event_identity(
                source_id,
                CURRENT_SEASON,
                match_id,
                bool(mapping.get("orientation_swapped")),
                "evaluator_exact_opponents_and_kickoff",
                "high",
                payload,
            ):
                accepted_ids.add(source_id)

        partial_ids = {
            _safe_int(row.get("event_id"))
            for row in friendly.get("partial_candidates_not_counted_as_matches", [])
            if isinstance(row, Mapping)
        }
        for source_id, payload in friendly_events.items():
            if source_id in accepted_ids:
                continue
            reason = (
                "loose_or_partial_event_identity"
                if source_id in partial_ids
                else "api_event_without_exact_identity"
            )
            self._reject("event_identity", CURRENT_SEASON, reason, "")
        for row in friendly.get("repo_only", []):
            if isinstance(row, Mapping):
                self._reject(
                    "canonical_match",
                    CURRENT_SEASON,
                    "canonical_match_without_provider_identity",
                    str(row.get("match_id") or ""),
                )
        self.db.commit()

    def _insert_player_identity(
        self,
        source_player_id: Any,
        season: str,
        player_id: Any,
        method: str,
        confidence: str,
    ) -> bool:
        source_id = _safe_int(source_player_id)
        canonical = _canonical_id(player_id)
        if source_id is None or not canonical:
            return False
        if str(confidence).casefold() != "high":
            self._reject("player_identity", season, "low_confidence_identity", canonical)
            return False
        if canonical not in self.players_by_id.get(season, {}):
            self._reject("player_identity", season, "canonical_player_missing", canonical)
            return False
        try:
            self.db.execute(
                """
                INSERT INTO source_player_identity
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (SOURCE_NAME, source_id, season, canonical, method, confidence),
            )
        except sqlite3.IntegrityError:
            existing = self.db.execute(
                """
                SELECT player_id FROM source_player_identity
                WHERE data_source = ? AND source_player_id = ? AND season = ?
                """,
                (SOURCE_NAME, source_id, season),
            ).fetchone()
            if not existing or existing[0] != canonical:
                self._reject("player_identity", season, "conflicting_identity", canonical)
            return False
        return True

    @staticmethod
    def _identity_rows(value: Any) -> list[Mapping[str, Any]]:
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
        if isinstance(value, Mapping):
            for key in (
                "mappings",
                "identities",
                "players",
                "accepted",
                "rows",
                "player_identity_map",
            ):
                rows = value.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, Mapping)]
        return []

    def _load_player_identities(self) -> None:
        identity_files = (
            ("player_identity_map.json", HISTORICAL_SEASON),
            ("friendly_player_identity_map.json", CURRENT_SEASON),
            ("friendlies_player_identity_map.json", CURRENT_SEASON),
        )
        candidates: dict[tuple[str, int, str], tuple[str, str]] = {}
        for filename, default_season in identity_files:
            for row in self._identity_rows(_load_json(SAMPLE_DIR / filename, [])):
                season = str(row.get("season") or default_season)
                source_id = _safe_int(
                    row.get("provider_player_id")
                    or row.get("source_player_id")
                    or row.get("bzzoiro_player_id")
                )
                player_id = _canonical_id(
                    row.get("fpl_player_id") or row.get("canonical_player_id")
                )
                confidence = str(row.get("confidence") or "").casefold()
                method = str(
                    row.get("method")
                    or row.get("match_method")
                    or "evaluator_identity"
                )
                if source_id is None or not player_id:
                    self._reject(
                        "player_identity", season, "incomplete_identity", player_id
                    )
                    continue
                if confidence != "high":
                    self._reject(
                        "player_identity", season, "low_confidence_identity", player_id
                    )
                    continue
                candidates[(season, source_id, player_id)] = (method, confidence)

        by_source: dict[tuple[str, int], set[str]] = defaultdict(set)
        by_canonical: dict[tuple[str, str], set[int]] = defaultdict(set)
        for season, source_id, player_id in candidates:
            by_source[(season, source_id)].add(player_id)
            by_canonical[(season, player_id)].add(source_id)

        for (season, source_id, player_id), (method, confidence) in sorted(
            candidates.items(), key=lambda item: item[0]
        ):
            if len(by_source[(season, source_id)]) != 1:
                self._reject(
                    "player_identity", season, "ambiguous_provider_identity", player_id
                )
                continue
            if len(by_canonical[(season, player_id)]) != 1:
                self._reject(
                    "player_identity", season, "ambiguous_canonical_identity", player_id
                )
                continue
            self._insert_player_identity(
                source_id, season, player_id, method, confidence
            )
        self.db.commit()

    def _load_event_artifacts(self) -> None:
        for path in sorted(SAMPLE_DIR.glob("*event_*_stats.json")):
            payload = _load_json(path, {})
            event_id = _safe_int(
                payload.get("event_id") if isinstance(payload, Mapping) else None
            )
            if event_id is None:
                match = re.search(r"event_(\d+)_stats", path.stem)
                event_id = _safe_int(match.group(1)) if match else None
            if event_id is not None and isinstance(payload, Mapping):
                self.event_stats[event_id] = dict(payload)
        for path in sorted(SAMPLE_DIR.glob("*event_*_lineups.json")):
            payload = _load_json(path, {})
            event_id = _safe_int(
                payload.get("event_id") if isinstance(payload, Mapping) else None
            )
            if event_id is not None and isinstance(payload, Mapping):
                self.event_lineups[event_id] = dict(payload)
        for path in sorted(SAMPLE_DIR.glob("*event_*_incidents.json")):
            payload = _load_json(path, {})
            event_id = _safe_int(
                payload.get("event_id") if isinstance(payload, Mapping) else None
            )
            if event_id is not None and isinstance(payload, Mapping):
                self.event_incidents[event_id] = dict(payload)
        for path in sorted(SAMPLE_DIR.glob("event_*_detail.json")):
            payload = _load_json(path, {})
            event_id = _safe_int(payload.get("id") if isinstance(payload, Mapping) else None)
            if event_id is not None and isinstance(payload, Mapping):
                self.event_payloads[event_id] = dict(payload)
                if self._event_identity(event_id):
                    self.db.execute(
                        """
                        INSERT OR REPLACE INTO staged_event
                        VALUES (?, ?, ?)
                        """,
                        (SOURCE_NAME, event_id, _json_text(payload)),
                    )
        self.db.commit()

    def _event_identity(self, event_id: Any) -> tuple[str, str, bool] | None:
        source_id = _safe_int(event_id)
        if source_id is None:
            return None
        row = self.db.execute(
            """
            SELECT season, match_id, orientation_swapped
            FROM source_event_identity
            WHERE data_source = ? AND source_event_id = ?
            """,
            (SOURCE_NAME, source_id),
        ).fetchone()
        if not row:
            return None
        return str(row[0]), str(row[1]), bool(row[2])

    def _player_identity(self, source_player_id: Any, season: str) -> str | None:
        source_id = _safe_int(source_player_id)
        if source_id is None:
            return None
        row = self.db.execute(
            """
            SELECT player_id FROM source_player_identity
            WHERE data_source = ? AND source_player_id = ? AND season = ?
            """,
            (SOURCE_NAME, source_id, season),
        ).fetchone()
        return str(row[0]) if row else None

    def _stage_player_stats(self) -> None:
        patterns = ("player_stats_event_*.json", "friendly_player_stats_event_*.json")
        paths: set[Path] = set()
        for pattern in patterns:
            paths.update(SAMPLE_DIR.glob(pattern))
        for path in sorted(paths, key=lambda item: item.name):
            payload = _load_json(path, [])
            rows = self._identity_rows(payload)
            for raw in rows:
                event_id = _safe_int(raw.get("event_id"))
                source_player_id = _safe_int(raw.get("player_id"))
                event_identity = self._event_identity(event_id)
                if not event_identity:
                    self._reject("player_match_stat", "", "unmapped_event", "")
                    continue
                season, match_id, _ = event_identity
                player_id = self._player_identity(source_player_id, season)
                if not player_id:
                    self._reject(
                        "player_match_stat", season, "unmapped_player", match_id
                    )
                    continue
                match = self._match_row(season, match_id)
                player = self.players_by_id[season].get(player_id, {})
                valid_team_codes = {
                    _canonical_id(match.get("home_team")),
                    _canonical_id(match.get("away_team")),
                }
                player_team = _canonical_id(player.get("team_code"))
                if player_team and player_team not in valid_team_codes:
                    self._reject(
                        "player_match_stat",
                        season,
                        "player_not_on_canonical_fixture_team",
                        match_id,
                    )
                    continue
                try:
                    self.db.execute(
                        """
                        INSERT OR REPLACE INTO staged_player_match_stat
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            SOURCE_NAME,
                            event_id,
                            source_player_id,
                            season,
                            _json_text(raw),
                        ),
                    )
                except sqlite3.IntegrityError:
                    self._reject(
                        "player_match_stat", season, "staging_constraint_failed", match_id
                    )
                    continue
                self.stage_stats.append(
                    {
                        "season": season,
                        "match_id": match_id,
                        "player_id": player_id,
                        "player_name": player.get("web_name")
                        or player.get("second_name")
                        or player_id,
                        "raw": dict(raw),
                    }
                )
        self.db.commit()

    def _match_row(self, season: str, match_id: str) -> dict[str, Any]:
        for row in self.context_rows[season]["matches"]:
            if str(row.get("match_id")) == match_id:
                return row
        raise KeyError((season, match_id))

    def _reject(self, entity: str, season: str, reason: str, canonical_id: str) -> None:
        self.db.execute(
            "INSERT INTO rejected_row VALUES (?, ?, ?, ?)",
            (entity, season or "", reason, canonical_id or ""),
        )

    def _write_context_exports(self) -> None:
        for context in CONTEXTS:
            source = self.context_rows[context.season]
            export_dir = self.output_root / "git_export" / context.export_rel
            merged_matches, match_fills = self._merge_matches(context.season)
            matches_path = export_dir / "matches.csv"
            match_count = _write_csv(matches_path, source["match_columns"], merged_matches)
            fixtures_path = export_dir / "fixtures.csv"
            fixtures_path.write_bytes(matches_path.read_bytes())
            merged_pms, pms_added, pms_fills = self._merge_pms(context.season)
            pms_count = _write_csv(
                export_dir / "playermatchstats.csv",
                source["pms_columns"],
                merged_pms,
            )
            extras = self._player_enrichment(context.season)
            _write_csv(
                export_dir / "bzzoiro_player_match_enrichment.csv",
                PLAYER_ENRICHMENT_COLUMNS,
                extras,
            )
            match_extras = self._match_enrichment(context.season)
            _write_csv(
                export_dir / "bzzoiro_match_enrichment.csv",
                MATCH_ENRICHMENT_COLUMNS,
                match_extras,
            )
            _write_csv(
                export_dir / "bzzoiro_shots.csv",
                SHOT_COLUMNS,
                self._shots(context.season),
            )
            _write_csv(
                export_dir / "bzzoiro_lineups.csv",
                LINEUP_COLUMNS,
                self._lineups(context.season),
            )
            _write_csv(
                export_dir / "bzzoiro_incidents.csv",
                INCIDENT_COLUMNS,
                self._incidents(context.season),
            )
            _write_csv(
                export_dir / "bzzoiro_momentum.csv",
                MOMENTUM_COLUMNS,
                self._momentum(context.season),
            )
            _write_csv(
                export_dir / "bzzoiro_xg_by_minute.csv",
                XG_MINUTE_COLUMNS,
                self._xg_by_minute(context.season),
            )
            _write_csv(
                export_dir / "bzzoiro_average_positions.csv",
                AVERAGE_POSITION_COLUMNS,
                self._average_positions(context.season),
            )
            accepted_stats = sum(
                1 for row in self.stage_stats if row["season"] == context.season
            )
            self.merge_metrics.extend(
                [
                    {
                        "season": context.season,
                        "target": "matches.csv",
                        "canonical_rows": len(source["matches"]),
                        "accepted_provider_rows": self._accepted_event_count(context.season),
                        "rows_added": 0,
                        "blank_cells_filled": match_fills,
                        "supplemental_rows": len(match_extras),
                    },
                    {
                        "season": context.season,
                        "target": "playermatchstats.csv",
                        "canonical_rows": len(source["pms"]),
                        "accepted_provider_rows": accepted_stats,
                        "rows_added": pms_added,
                        "blank_cells_filled": pms_fills,
                        "supplemental_rows": len(extras),
                    },
                ]
            )
            self.row_counts[str(matches_path.relative_to(self.output_root)).replace("\\", "/")] = match_count
            self.row_counts[str(fixtures_path.relative_to(self.output_root)).replace("\\", "/")] = match_count
            self.row_counts[
                str((export_dir / "playermatchstats.csv").relative_to(self.output_root)).replace("\\", "/")
            ] = pms_count

    def _accepted_event_count(self, season: str) -> int:
        return int(
            self.db.execute(
                "SELECT COUNT(*) FROM source_event_identity WHERE season = ?",
                (season,),
            ).fetchone()[0]
        )

    def _accepted_events(self, season: str) -> list[tuple[int, str, bool]]:
        return [
            (int(row[0]), str(row[1]), bool(row[2]))
            for row in self.db.execute(
                """
                SELECT source_event_id, match_id, orientation_swapped
                FROM source_event_identity
                WHERE season = ?
                ORDER BY match_id
                """,
                (season,),
            )
        ]

    def _merge_matches(self, season: str) -> tuple[list[dict[str, Any]], int]:
        rows = [dict(row) for row in self.context_rows[season]["matches"]]
        by_id = {str(row["match_id"]): row for row in rows}
        fills = 0
        for event_id, match_id, swapped in self._accepted_events(season):
            row = by_id[match_id]
            event = self.event_payloads.get(event_id, {})
            home_prefix, away_prefix = ("away", "home") if swapped else ("home", "away")
            basic = {
                "kickoff_time": event.get("event_date"),
                "finished": (
                    True
                    if str(event.get("status", "")).lower() == "finished"
                    else False
                    if str(event.get("status", "")).lower() in {"notstarted", "scheduled"}
                    else None
                ),
                "home_score": event.get(f"{home_prefix}_score"),
                "away_score": event.get(f"{away_prefix}_score"),
            }
            for column, value in basic.items():
                fills += int(_set_if_blank(row, column, value))
            stats = self.event_stats.get(event_id, {}).get("stats", {})
            if isinstance(stats, Mapping):
                canonical_home = stats.get(home_prefix, {})
                canonical_away = stats.get(away_prefix, {})
                if (
                    self._team_stats_complete(canonical_home)
                    and self._team_stats_complete(canonical_away)
                ):
                    for side, values in (("home", canonical_home), ("away", canonical_away)):
                        if isinstance(values, Mapping):
                            fills += self._merge_team_stats(row, side, values)
                elif any(
                    isinstance(values, Mapping) and values
                    for values in (canonical_home, canonical_away)
                ):
                    self._reject(
                        "match_stat",
                        season,
                        "incomplete_team_stats",
                        match_id,
                    )
        return rows, fills

    @staticmethod
    def _team_stats_complete(stats: Any) -> bool:
        if not isinstance(stats, Mapping):
            return False
        required = ("total_shots", "passes", "accurate_passes")
        return all(field in stats and stats[field] is not None for field in required)

    @staticmethod
    def _merge_team_stats(
        row: dict[str, Any], side: str, stats: Mapping[str, Any]
    ) -> int:
        mappings: dict[str, Any] = {
            "possession": stats.get("ball_possession"),
            "expected_goals_xg": stats.get("expected_goals")
            if stats.get("expected_goals") is not None
            else _nested(stats, "xg", "actual"),
            "total_shots": stats.get("total_shots"),
            "shots_on_target": stats.get("shots_on_target"),
            "big_chances": stats.get("big_chances"),
            "big_chances_missed": stats.get("big_chances_missed"),
            "accurate_passes": stats.get("accurate_passes"),
            "accurate_passes_pct": stats.get("pass_accuracy_pct"),
            "fouls_committed": stats.get("fouls"),
            "corners": stats.get("corner_kicks"),
            "shots_off_target": stats.get("shots_off_target"),
            "blocked_shots": stats.get("blocked_shots"),
            "hit_woodwork": stats.get("hit_woodwork"),
            "shots_inside_box": stats.get("shots_inside_box"),
            "shots_outside_box": stats.get("shots_outside_box"),
            "passes": stats.get("passes"),
            "accurate_long_balls": _nested(stats, "long_balls", "value"),
            "accurate_long_balls_pct": _nested(stats, "long_balls", "pct"),
            "accurate_crosses": _nested(stats, "crosses", "value"),
            "accurate_crosses_pct": _nested(stats, "crosses", "pct"),
            "throws": stats.get("throw_ins"),
            "touches_in_opposition_box": stats.get("touches_in_penalty_area"),
            "offsides": stats.get("offsides"),
            "yellow_cards": stats.get("yellow_cards"),
            "red_cards": stats.get("red_cards"),
            # Provider tackles_won is observed as a percentage, never a count.
            "tackles_won_pct": stats.get("tackles_won"),
            "interceptions": stats.get("interceptions"),
            "clearances": stats.get("clearances"),
            "keeper_saves": stats.get("goalkeeper_saves"),
            "ground_duels_won": _nested(stats, "ground_duels", "value"),
            "ground_duels_won_pct": _nested(stats, "ground_duels", "pct"),
            "aerial_duels_won": _nested(stats, "aerial_duels", "value"),
            "aerial_duels_won_pct": _nested(stats, "aerial_duels", "pct"),
            "successful_dribbles": _nested(stats, "dribbles", "value"),
            "successful_dribbles_pct": _nested(stats, "dribbles", "pct"),
        }
        fills = 0
        for suffix, value in mappings.items():
            fills += int(_set_if_blank(row, f"{side}_{suffix}", value))
        return fills

    def _merge_pms(
        self, season: str
    ) -> tuple[list[dict[str, Any]], int, int]:
        source = self.context_rows[season]
        columns = source["pms_columns"]
        rows = [dict(row) for row in source["pms"]]
        by_key = {
            (_canonical_id(row.get("player_id")), str(row.get("match_id"))): row
            for row in rows
        }
        added = 0
        fills = 0
        for staged in sorted(
            (row for row in self.stage_stats if row["season"] == season),
            key=lambda row: (row["match_id"], int(row["player_id"])),
        ):
            key = (staged["player_id"], staged["match_id"])
            target = by_key.get(key)
            if target is None:
                target = {column: "" for column in columns}
                target["player_id"] = staged["player_id"]
                target["match_id"] = staged["match_id"]
                rows.append(target)
                by_key[key] = target
                added += 1
            raw = staged["raw"]
            for provider_field, canonical_field in PMS_DIRECT_SAFE.items():
                # blocked_scoring_attempt is intentionally absent: it is an
                # attacking shot blocked, not the defender blocks metric.
                fills += int(
                    _set_if_blank(target, canonical_field, raw.get(provider_field))
                )
        return rows, added, fills

    def _player_enrichment(self, season: str) -> list[dict[str, Any]]:
        extras: list[dict[str, Any]] = []
        mapping = {
            "rating": "rating",
            "possession_lost": "possession_lost",
            "blocked_scoring_attempt": "attacking_shots_blocked",
            "total_pass": "total_passes",
            "total_long_balls": "total_long_balls",
            "total_cross": "total_crosses",
            "total_contest": "total_dribbles",
            "duel_lost": "ground_duels_lost",
            "aerial_lost": "aerial_duels_lost",
            "yellow_card": "yellow_cards",
            "red_card": "red_cards",
            "punches": "goalkeeper_punches",
            "penalty_won": "penalties_won",
            "penalty_conceded": "penalties_conceded",
            "penalty_faced": "penalties_faced",
            "penalty_save": "penalties_saved",
        }
        for staged in sorted(
            (row for row in self.stage_stats if row["season"] == season),
            key=lambda row: (row["match_id"], int(row["player_id"])),
        ):
            raw = staged["raw"]
            out = {
                "player_id": staged["player_id"],
                "match_id": staged["match_id"],
                "player_name": staged["player_name"],
            }
            for provider_field, column in mapping.items():
                out[column] = raw.get(provider_field)
            extras.append(out)
        return extras

    def _match_enrichment(self, season: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event_id, match_id, _ in self._accepted_events(season):
            event = self.event_payloads.get(event_id, {})
            lineup = self.event_lineups.get(event_id, {})
            weather = event.get("weather") if isinstance(event.get("weather"), Mapping) else {}
            confidences = []
            sides = _nested(lineup, "lineups") or {}
            if isinstance(sides, Mapping):
                for side in ("home", "away"):
                    value = _nested(sides, side, "confidence")
                    if isinstance(value, (int, float)):
                        confidences.append(float(value))
            row = {
                "match_id": match_id,
                "travel_distance_km": event.get("travel_distance_km"),
                "weather_description": weather.get("description"),
                "temperature_c": weather.get("temperature_c"),
                "wind_speed": weather.get("wind_speed"),
                "pitch_condition": event.get("pitch_condition"),
                "is_local_derby": event.get("is_local_derby"),
                "is_neutral_ground": event.get("is_neutral_ground"),
                "attendance": event.get("attendance"),
                "lineup_status": lineup.get("lineup_status"),
                "lineup_confidence": (
                    sum(confidences) / len(confidences) if confidences else None
                ),
            }
            if any(not _is_blank(row[column]) for column in MATCH_ENRICHMENT_COLUMNS[1:]):
                rows.append(row)
        return rows

    def _shots(self, season: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event_id, match_id, swapped in self._accepted_events(season):
            shots = self.event_stats.get(event_id, {}).get("shotmap", [])
            if not isinstance(shots, list):
                continue
            for index, shot in enumerate(shots, 1):
                if not isinstance(shot, Mapping):
                    continue
                raw_home = shot.get("home")
                is_home = (not bool(raw_home)) if swapped and raw_home is not None else raw_home
                source_player_id = shot.get("player_id")
                player_id = self._player_identity(source_player_id, season)
                player_name = ""
                if player_id:
                    player = self.players_by_id[season].get(player_id, {})
                    player_name = player.get("web_name") or player.get("second_name") or ""
                rows.append(
                    {
                        "match_id": match_id,
                        "shot_index": index,
                        "minute": _first_present(shot.get("min"), shot.get("minute")),
                        "added_time": shot.get("added_time"),
                        "is_home": is_home,
                        "player_id": player_id,
                        "player_name": player_name,
                        "outcome": shot.get("type"),
                        "situation": shot.get("sit") or shot.get("situation"),
                        "body_part": shot.get("body"),
                        "xg": shot.get("xg"),
                        "xgot": shot.get("xgot"),
                        "start_x": _nested(shot, "pos", "x"),
                        "start_y": _nested(shot, "pos", "y"),
                        "goal_mouth_y": _nested(shot, "gm", "y"),
                        "goal_mouth_z": _nested(shot, "gm", "z"),
                        "goal_mouth_location": shot.get("gml"),
                    }
                )
        return rows

    def _lineups(self, season: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event_id, match_id, swapped in self._accepted_events(season):
            payload = self.event_lineups.get(event_id, {})
            lineups = payload.get("lineups", {})
            if not isinstance(lineups, Mapping):
                continue
            match = self._match_row(season, match_id)
            for raw_side in ("home", "away"):
                side_data = lineups.get(raw_side, {})
                if not isinstance(side_data, Mapping):
                    continue
                canonical_side = (
                    "away" if raw_side == "home" else "home"
                ) if swapped else raw_side
                team_code = _canonical_id(match.get(f"{canonical_side}_team"))
                for collection, starting in (("players", True), ("substitutes", False)):
                    players = side_data.get(collection, [])
                    if not isinstance(players, list):
                        continue
                    for player in players:
                        if not isinstance(player, Mapping):
                            continue
                        player_id = self._player_identity(player.get("id"), season)
                        if not player_id:
                            player_id = self._match_lineup_name(
                                season, team_code, player.get("name")
                            )
                        rows.append(
                            {
                                "match_id": match_id,
                                "team_side": canonical_side,
                                "team_code": team_code,
                                "player_id": player_id,
                                "player_name": player.get("name")
                                or player.get("short_name"),
                                "position": player.get("position"),
                                "jersey_number": player.get("jersey_number"),
                                "is_starting": starting,
                                "formation": side_data.get("formation"),
                                "lineup_status": payload.get("lineup_status"),
                                "confidence": side_data.get("confidence"),
                            }
                        )
        return rows

    def _match_lineup_name(self, season: str, team_code: str, name: Any) -> str | None:
        normal = _normalise_name(name)
        if not normal or not team_code:
            return None
        candidates = self.players_by_team_name[season].get((team_code, normal), [])
        return candidates[0] if len(candidates) == 1 else None

    def _incidents(self, season: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event_id, match_id, swapped in self._accepted_events(season):
            incidents = self.event_incidents.get(event_id, {}).get("incidents", [])
            if not isinstance(incidents, list):
                continue
            for index, incident in enumerate(incidents, 1):
                if not isinstance(incident, Mapping):
                    continue
                raw_is_home = incident.get("is_home")
                if raw_is_home is None:
                    side = ""
                else:
                    canonical_home = (not bool(raw_is_home)) if swapped else bool(raw_is_home)
                    side = "home" if canonical_home else "away"
                primary_source = (
                    incident.get("player_id")
                    or incident.get("player_out_id")
                    or incident.get("player_in_id")
                )
                secondary_source = incident.get("player_in_id")
                assist_source = incident.get("assist_id") or incident.get("assist_player_id")
                primary_name = (
                    incident.get("player")
                    or incident.get("player_out")
                    or incident.get("player_in")
                )
                secondary_name = incident.get("player_in")
                assist_name = incident.get("assist")
                home_score, away_score = incident.get("home_score"), incident.get("away_score")
                if swapped:
                    home_score, away_score = away_score, home_score
                rows.append(
                    {
                        "match_id": match_id,
                        "incident_index": index,
                        "incident_type": incident.get("type"),
                        "minute": incident.get("minute"),
                        "added_time": incident.get("added_time"),
                        "team_side": side,
                        "player_id": self._player_identity(primary_source, season),
                        "player_name": primary_name,
                        "secondary_player_id": self._player_identity(
                            secondary_source, season
                        ),
                        "secondary_player_name": secondary_name,
                        "assist_player_id": self._player_identity(assist_source, season),
                        "assist_player_name": assist_name,
                        "card_type": incident.get("card_type"),
                        "goal_type": incident.get("goal_type"),
                        "home_score": home_score,
                        "away_score": away_score,
                        "text": incident.get("text"),
                    }
                )
        return rows

    def _momentum(self, season: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event_id, match_id, swapped in self._accepted_events(season):
            momentum = self.event_stats.get(event_id, {}).get("momentum", [])
            if not isinstance(momentum, list):
                continue
            for point in momentum:
                if isinstance(point, Mapping):
                    value = _first_present(point.get("v"), point.get("value"))
                    if swapped and isinstance(value, (int, float)):
                        value = -value
                    rows.append(
                        {
                            "match_id": match_id,
                            "minute": _first_present(point.get("m"), point.get("minute")),
                            "value": value,
                        }
                    )
        return rows

    def _xg_by_minute(self, season: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event_id, match_id, swapped in self._accepted_events(season):
            points = self.event_stats.get(event_id, {}).get("xg_per_minute", [])
            if not isinstance(points, list):
                continue
            for point in points:
                if not isinstance(point, Mapping):
                    continue
                home_xg, away_xg = point.get("xg_home"), point.get("xg_away")
                cum_home, cum_away = point.get("cum_home"), point.get("cum_away")
                if swapped:
                    home_xg, away_xg = away_xg, home_xg
                    cum_home, cum_away = cum_away, cum_home
                rows.append(
                    {
                        "match_id": match_id,
                        "minute": _first_present(point.get("m"), point.get("minute")),
                        "home_xg": home_xg,
                        "away_xg": away_xg,
                        "home_cumulative_xg": cum_home,
                        "away_cumulative_xg": cum_away,
                    }
                )
        return rows

    def _average_positions(self, season: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event_id, match_id, swapped in self._accepted_events(season):
            positions = self.event_stats.get(event_id, {}).get("average_positions", {})
            if not isinstance(positions, Mapping):
                continue
            for raw_side in ("home", "away"):
                entries = positions.get(raw_side, [])
                if not isinstance(entries, list):
                    continue
                canonical_side = (
                    "away" if raw_side == "home" else "home"
                ) if swapped else raw_side
                for entry in entries:
                    if not isinstance(entry, Mapping):
                        continue
                    player_id = self._player_identity(entry.get("player_id"), season)
                    rows.append(
                        {
                            "match_id": match_id,
                            "team_side": canonical_side,
                            "player_id": player_id,
                            "player_name": entry.get("name"),
                            "jersey_number": entry.get("n"),
                            "position": entry.get("pos"),
                            "x": entry.get("x"),
                            "y": entry.get("y"),
                        }
                    )
        return rows

    def _write_supplemental_exports(self) -> None:
        directory = self.output_root / "git_export" / "supplemental"
        availability: list[dict[str, Any]] = []
        profiles = self._identity_rows(
            _load_json(SAMPLE_DIR / "player_profile_sample.json", [])
        )
        for row in profiles:
            source_id = row.get("provider_player_id")
            player_id = self._player_identity(source_id, CURRENT_SEASON)
            if not player_id:
                self._reject(
                    "player_availability",
                    CURRENT_SEASON,
                    "unmapped_player",
                    "",
                )
                continue
            player = self.players_by_id[CURRENT_SEASON].get(player_id, {})
            availability.append(
                {
                    "season": CURRENT_SEASON,
                    "player_id": player_id,
                    "player_name": player.get("web_name") or player.get("second_name"),
                    "bzzoiro_availability": row.get("provider_availability"),
                    "bzzoiro_injury_risk": row.get("provider_injury_risk"),
                    "contract_until": row.get("contract_until"),
                    "market_value_eur": row.get("market_value_eur"),
                }
            )
        _write_csv(
            directory / "bzzoiro_player_availability.csv",
            AVAILABILITY_COLUMNS,
            sorted(availability, key=lambda row: (row["season"], int(row["player_id"]))),
        )

        competitions: list[dict[str, Any]] = []
        for league in _load_json(SAMPLE_DIR / "leagues.json", []):
            if not isinstance(league, Mapping):
                continue
            current = (
                league.get("current_season")
                if isinstance(league.get("current_season"), Mapping)
                else {}
            )
            competitions.append(
                {
                    "competition_name": league.get("name"),
                    "country": league.get("country"),
                    "is_women": league.get("is_women"),
                    "is_active": league.get("is_active"),
                    "current_season_name": current.get("name"),
                    "current_season_year": current.get("year"),
                    "season_start": current.get("start_date"),
                    "season_end": current.get("end_date"),
                }
            )
        _write_csv(
            directory / "bzzoiro_competitions.csv",
            COMPETITION_COLUMNS,
            sorted(
                competitions,
                key=lambda row: (
                    str(row.get("country") or ""),
                    str(row.get("competition_name") or ""),
                ),
            ),
        )

        odds = self._public_odds()
        predictions = self._public_predictions()
        _write_csv(directory / "bzzoiro_odds.csv", ODDS_COLUMNS, odds)
        _write_csv(
            directory / "bzzoiro_predictions.csv",
            PREDICTION_COLUMNS,
            predictions,
        )
        self.db.commit()

    def _public_odds(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for event in _load_json(SAMPLE_DIR / "odds_best_sample.json", []):
            if not isinstance(event, Mapping):
                continue
            identity = self._event_identity(event.get("event_id"))
            outcomes = event.get("best_odds", [])
            if not identity:
                self._reject("betting_odds", "", "unmapped_event", "")
                continue
            _, match_id, swapped = identity
            home_team, away_team = _canonical_sides(
                event.get("home_team"), event.get("away_team"), swapped
            )
            if not isinstance(outcomes, list):
                continue
            for outcome in outcomes:
                if not isinstance(outcome, Mapping):
                    continue
                canonical_outcome = _canonical_outcome(
                    outcome.get("outcome"), swapped
                )
                outcome_label = str(canonical_outcome or "").upper()
                outcome_name = outcome.get("outcome_name")
                if outcome_label in {"H", "HOME"}:
                    outcome_name = home_team
                elif outcome_label in {"A", "AWAY"}:
                    outcome_name = away_team
                elif outcome_label in {"D", "DRAW"}:
                    outcome_name = "Draw"
                key = (
                    match_id,
                    event.get("market"),
                    canonical_outcome,
                    outcome.get("line"),
                    outcome.get("bookmaker_slug"),
                    outcome.get("updated_at"),
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "match_id": match_id,
                        "kickoff_time": event.get("event_date"),
                        "competition": event.get("league_name"),
                        "home_team": home_team,
                        "away_team": away_team,
                        "market": event.get("market"),
                        "outcome": canonical_outcome,
                        "line": outcome.get("line"),
                        "outcome_name": outcome_name,
                        "decimal_odds": outcome.get("decimal_odds"),
                        "bookmaker": outcome.get("bookmaker_name")
                        or outcome.get("bookmaker_slug"),
                        "odds_updated_at": outcome.get("updated_at"),
                    }
                )
        return sorted(
            rows,
            key=lambda row: (
                row["match_id"],
                str(row["market"]),
                str(row["outcome"]),
                str(row["bookmaker"]),
            ),
        )

    def _public_predictions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for prediction in _load_json(SAMPLE_DIR / "predictions_sample.json", []):
            if not isinstance(prediction, Mapping):
                continue
            event = prediction.get("event", {})
            if not isinstance(event, Mapping):
                continue
            identity = self._event_identity(event.get("id"))
            if not identity:
                self._reject("betting_prediction", "", "unmapped_event", "")
                continue
            _, match_id, swapped = identity
            home_team, away_team = _canonical_sides(
                event.get("home_team"), event.get("away_team"), swapped
            )
            markets = prediction.get("markets", {})
            recommendations = prediction.get("recommendations", {})
            model = prediction.get("model", {})
            result = markets.get("match_result", {}) if isinstance(markets, Mapping) else {}
            over = markets.get("over_under", {}) if isinstance(markets, Mapping) else {}
            btts = markets.get("btts", {}) if isinstance(markets, Mapping) else {}
            prob_home, prob_away = _canonical_sides(
                result.get("prob_home"), result.get("prob_away"), swapped
            ) if isinstance(result, Mapping) else (None, None)
            predicted_result = _canonical_outcome(
                result.get("predicted"), swapped
            ) if isinstance(result, Mapping) else None
            recommended_favorite = _canonical_outcome(
                recommendations.get("favorite"), swapped
            ) if isinstance(recommendations, Mapping) else None
            rows.append(
                {
                    "match_id": match_id,
                    "kickoff_time": event.get("event_date"),
                    "competition": event.get("league_name"),
                    "home_team": home_team,
                    "away_team": away_team,
                    "prediction_created_at": prediction.get("created_at"),
                    "model_version": model.get("version") if isinstance(model, Mapping) else None,
                    "model_confidence": model.get("confidence") if isinstance(model, Mapping) else None,
                    "predicted_result": predicted_result,
                    "prob_home": prob_home,
                    "prob_draw": result.get("prob_draw") if isinstance(result, Mapping) else None,
                    "prob_away": prob_away,
                    "prob_over_15": over.get("prob_over_15") if isinstance(over, Mapping) else None,
                    "prob_over_25": over.get("prob_over_25") if isinstance(over, Mapping) else None,
                    "prob_over_35": over.get("prob_over_35") if isinstance(over, Mapping) else None,
                    "prob_btts_yes": btts.get("prob_yes") if isinstance(btts, Mapping) else None,
                    "recommended_favorite": recommended_favorite,
                    "recommend_over_15": recommendations.get("over_15")
                    if isinstance(recommendations, Mapping)
                    else None,
                    "recommend_over_25": recommendations.get("over_25")
                    if isinstance(recommendations, Mapping)
                    else None,
                    "recommend_over_35": recommendations.get("over_35")
                    if isinstance(recommendations, Mapping)
                    else None,
                    "recommend_btts": recommendations.get("btts")
                    if isinstance(recommendations, Mapping)
                    else None,
                }
            )
        return sorted(rows, key=lambda row: (row["match_id"], str(row["prediction_created_at"])))

    def _write_audits(self) -> None:
        directory = self.output_root / "audit"
        identities: list[dict[str, Any]] = []
        for row in self.db.execute(
            """
            SELECT 'event', e.season, e.match_id, m.canonical_name,
                   e.resolution_method, e.confidence
            FROM source_event_identity e
            JOIN canonical_match m
              ON m.season = e.season AND m.match_id = e.match_id
            ORDER BY e.season, e.match_id
            """
        ):
            identities.append(
                dict(zip(IDENTITY_AUDIT_COLUMNS, row, strict=True))
            )
        for row in self.db.execute(
            """
            SELECT 'player', p.season, p.player_id, c.canonical_name,
                   p.resolution_method, p.confidence
            FROM source_player_identity p
            JOIN canonical_player c
              ON c.season = p.season AND c.player_id = p.player_id
            ORDER BY p.season, CAST(p.player_id AS INTEGER)
            """
        ):
            identities.append(
                dict(zip(IDENTITY_AUDIT_COLUMNS, row, strict=True))
            )
        _write_csv(
            directory / "accepted_identities.csv",
            IDENTITY_AUDIT_COLUMNS,
            identities,
        )
        rejections = [
            dict(zip(REJECTION_SUMMARY_COLUMNS, row, strict=True))
            for row in self.db.execute(
                """
                SELECT entity, season, reason, COUNT(*)
                FROM rejected_row
                GROUP BY entity, season, reason
                ORDER BY entity, season, reason
                """
            )
        ]
        _write_csv(
            directory / "rejection_summary.csv",
            REJECTION_SUMMARY_COLUMNS,
            rejections,
        )
        _write_csv(
            directory / "merge_audit.csv",
            MERGE_AUDIT_COLUMNS,
            sorted(self.merge_metrics, key=lambda row: (row["season"], row["target"])),
        )
        schema_rows: list[dict[str, Any]] = []
        for context in CONTEXTS:
            directory_path = self.output_root / "git_export" / context.export_rel
            source_match_header = self.context_rows[context.season]["match_columns"]
            source_pms_header = self.context_rows[context.season]["pms_columns"]
            for filename, expected_header in (
                ("matches.csv", source_match_header),
                ("fixtures.csv", source_match_header),
                ("playermatchstats.csv", source_pms_header),
            ):
                path = directory_path / filename
                header, rows = _read_csv(path)
                schema_rows.append(
                    {
                        "file": str(path.relative_to(self.output_root)).replace("\\", "/"),
                        "row_count": len(rows),
                        "column_count": len(header),
                        "matches_canonical_header": header == expected_header,
                        "fixtures_byte_identical": (
                            (directory_path / "matches.csv").read_bytes()
                            == (directory_path / "fixtures.csv").read_bytes()
                            if filename in {"matches.csv", "fixtures.csv"}
                            else ""
                        ),
                    }
                )
        _write_csv(
            directory / "schema_audit.csv",
            SCHEMA_AUDIT_COLUMNS,
            schema_rows,
        )
        self.row_counts["audit/accepted_identities.csv"] = len(identities)
        self.row_counts["audit/rejection_summary.csv"] = len(rejections)
        self.row_counts["audit/merge_audit.csv"] = len(self.merge_metrics)
        self.row_counts["audit/schema_audit.csv"] = len(schema_rows)

    def _write_diff_summary(self) -> None:
        rejected = int(
            self.db.execute("SELECT COUNT(*) FROM rejected_row").fetchone()[0]
        )
        accepted_events = int(
            self.db.execute("SELECT COUNT(*) FROM source_event_identity").fetchone()[0]
        )
        accepted_players = int(
            self.db.execute("SELECT COUNT(*) FROM source_player_identity").fetchone()[0]
        )
        staged_stats = int(
            self.db.execute(
                "SELECT COUNT(*) FROM staged_player_match_stat"
            ).fetchone()[0]
        )
        summary = {
            "simulation": True,
            "canonical_data_tree_modified": False,
            "captured_snapshot": self.generated_from,
            "identity_resolution": {
                "accepted_events": accepted_events,
                "accepted_players": accepted_players,
                "accepted_player_match_rows": staged_stats,
                "quarantined_rows": rejected,
            },
            "merges": sorted(
                self.merge_metrics, key=lambda row: (row["season"], row["target"])
            ),
            "safety": {
                "existing_non_blank_values_win": True,
                "only_exact_event_identities_export": True,
                "fpl_availability_remains_authoritative": True,
                "provider_availability_is_separate": True,
                "attacking_blocked_shots_are_not_defender_blocks": True,
                "fixtures_equal_matches": True,
                "public_provenance_columns_emitted": False,
            },
        }
        path = self.output_root / "DIFF_SUMMARY.json"
        path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _write_readme(self) -> None:
        friendlies = len(self.context_rows[CURRENT_SEASON]["matches"])
        accepted_friendlies = self._accepted_event_count(CURRENT_SEASON)
        text = f"""# Bzzoiro integration review export

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

The friendlies mirror contains all **{friendlies}** canonical rows. Only
**{accepted_friendlies}** exact provider identities are eligible for
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
"""
        (self.output_root / "README.md").write_text(text, encoding="utf-8")

    def _write_manifest(self) -> None:
        files: list[dict[str, Any]] = []
        for path in sorted(
            (
                item
                for item in self.output_root.rglob("*")
                if item.is_file() and item.name != "MANIFEST.json"
            ),
            key=lambda item: item.as_posix(),
        ):
            rel = str(path.relative_to(self.output_root)).replace("\\", "/")
            row_count = None
            if path.suffix.lower() == ".csv":
                _, rows = _read_csv(path)
                row_count = len(rows)
            files.append(
                {
                    "path": rel,
                    "sha256": _hash_file(path),
                    "rows": row_count,
                }
            )
        manifest = {
            "schema_version": 1,
            "simulation": True,
            "offline": True,
            "captured_snapshot": self.generated_from,
            "files": files,
        }
        (self.output_root / "MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def build_review_export(output_root: Path = DEFAULT_OUTPUT) -> None:
    PreviewBuilder(output_root).build()


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): _hash_file(path)
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    }


def check_review_export(output_root: Path = DEFAULT_OUTPUT) -> tuple[bool, list[str]]:
    with tempfile.TemporaryDirectory(prefix="bzzoiro-review-check-") as temporary:
        candidate = Path(temporary) / "review_export"
        build_review_export(candidate)
        expected = _tree_hashes(candidate)
        actual = _tree_hashes(output_root) if output_root.exists() else {}
    changed = sorted(
        {
            *[f"missing:{path}" for path in expected.keys() - actual.keys()],
            *[f"unexpected:{path}" for path in actual.keys() - expected.keys()],
            *[
                f"changed:{path}"
                for path in expected.keys() & actual.keys()
                if expected[path] != actual[path]
            ],
        }
    )
    return not changed, changed


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="generated review directory (default: scripts/bzzoiro/review_export)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify that the existing review export is current",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    output = args.output
    if not output.is_absolute():
        output = (Path.cwd() / output).resolve()
    if args.check:
        ok, changes = check_review_export(output)
        if ok:
            print(f"review export is current: {output}")
            return 0
        print(f"review export is stale: {output}", file=sys.stderr)
        for change in changes:
            print(f"  {change}", file=sys.stderr)
        return 1
    build_review_export(output)
    print(f"wrote deterministic review export: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
