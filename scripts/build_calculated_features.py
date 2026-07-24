#!/usr/bin/env python3
"""
Build the `calculated_features` sheet for the Premier League gameweek data.

This is a self-contained, plug-and-play script (pandas / numpy / stdlib only)
that derives a set of leakage-safe, model-ready engineered features from the
raw per-gameweek Premier League tables and writes them back as a new
`calculated_features.csv` inside each Premier League gameweek folder.

Scope: PREMIER LEAGUE ONLY. Every feature is computed from Premier League
tables (player_gameweek_stats, players, playermatchstats, matches, fixtures);
team/opponent strength and rolling form are all PL-derived, so the sheet is
only written under `By Tournament/Premier League/GW*/` and is not meaningful
for the cup / European tournament folders.

Leakage safety: every rolling feature is `shift(1)`-lagged per player (or per
team) so the value on a GW N row only reflects gameweeks 1..N-1. Because of
this, each row's features can be joined to that same GW N and used to predict
GW N's outcome without leaking the target. GW1 rows therefore have NaN rolling
features by design (no prior gameweek exists) — this is expected, not a bug.

Usage (run from the repository root):

    python scripts/build_calculated_features.py
    python scripts/build_calculated_features.py --season 2025-2026
    python scripts/build_calculated_features.py --start-gw 1 --end-gw 20

Features are ALWAYS computed over the full available history (GW1..latest) so
rolling windows are correctly seeded; `--start-gw`/`--end-gw` only filter which
gameweeks' `calculated_features.csv` files get (re)written. So
`--start-gw 38 --end-gw 38` rewrites only GW38 — with fully-populated rolling
features, not an all-NaN sheet.

If `--season` is omitted, the latest `data/<season>` directory that contains
`By Tournament/Premier League/GW*` folders is used.
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Constants (mirrored from the reference feature pipeline)
# ---------------------------------------------------------------------------

# Position name mapping: local data -> standard FPL codes.
POSITION_MAP = {
    "Goalkeeper": "GKP",
    "Defender": "DEF",
    "Midfielder": "MID",
    "Forward": "FWD",
}

# Columns that must remain strings when reading raw CSVs (everything else is
# coerced to numeric with errors -> NaN). This mirrors the reference loader so
# that dtypes — and therefore all derived values — line up exactly.
STRING_COLS = {
    "id", "web_name", "first_name", "second_name", "status",
    "news", "news_added", "match_id", "match_url", "tournament",
    "corners_and_indirect_freekicks_text", "direct_freekicks_text",
    "penalties_text", "set_piece_threat", "player_code", "team_code",
    "position", "name", "short_name", "fotmob_name", "player_id",
    "kickoff_time", "home_team", "away_team",
}

# playermatchstats columns aggregated (summed) per (player_id, gameweek),
# renamed with a `match_` prefix.
MATCH_AGG_COLS = {
    "xg": "sum", "xa": "sum", "shots_on_target": "sum",
    "chances_created": "sum", "goals_prevented": "sum",
    "xgot_faced": "sum", "minutes_played": "sum",
    "touches_opposition_box": "sum",
}

# Final output column order (4 keys + 35 features).
OUTPUT_COLUMNS = [
    # keys
    "player_id", "gameweek", "position", "team_code",
    # rolling form
    "points_roll3", "points_roll5",
    "xg_roll3", "xg_roll5",
    "xa_roll3", "xa_roll5",
    "bps_roll3", "bps_roll5",
    "ict_roll3", "ict_roll5",
    "minutes_roll3", "minutes_roll5",
    "defcon_roll3", "defcon_roll5",
    "points_std5",
    # team / opponent strength
    "team_xg_for_roll5", "team_xg_against_roll5", "team_elo",
    "opp_xg_against_roll5", "opp_elo", "has_double_gw",
    # value / availability
    "ppm_roll5", "start_rate_roll5",
    # position-specific
    "gkp_saves_roll3",
    "def_clean_sheet_roll5", "def_attacking_return_rate",
    "mid_creativity_roll3", "mid_threat_roll3",
    "mid_shots_on_target_roll3", "mid_touches_box_roll3",
    "fwd_xg_roll3", "fwd_shots_on_target_roll3", "fwd_touches_box_roll3",
    # set-piece duty
    "is_penalty_taker", "is_corner_freekick_taker", "is_direct_freekick_taker",
]


# ---------------------------------------------------------------------------
# Raw data loading (mirrors the reference FPLDataLoader semantics)
# ---------------------------------------------------------------------------

def list_gameweeks(pl_dir: Path):
    """Return the sorted numeric gameweek numbers (GW1..GW38) present in `pl_dir`.

    Only numeric `GW<n>` folders with 1 <= n <= 38 are considered, so sibling
    folders such as a `GW0` friendlies bucket are ignored.
    """
    gws = []
    for d in pl_dir.iterdir():
        if not d.is_dir() or not d.name.startswith("GW"):
            continue
        suffix = d.name[2:]
        if not suffix.isdigit():
            continue
        gw = int(suffix)
        if 1 <= gw <= 38:
            gws.append(gw)
    return sorted(gws)


def load_gw_csv(pl_dir: Path, gw: int, file_type: str) -> pd.DataFrame:
    """Load a single `GW<gw>/<file_type>.csv`.

    Adds a `gameweek` column set to the folder's gameweek number (overriding any
    in-file gameweek column) and coerces every non-string column to numeric.
    Raises FileNotFoundError if the file is missing.
    """
    file_path = pl_dir / f"GW{gw}" / f"{file_type}.csv"
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    df = pd.read_csv(file_path)
    df["gameweek"] = gw

    for col in df.columns:
        if col not in STRING_COLS:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def load_range(pl_dir: Path, gws, file_type: str) -> pd.DataFrame:
    """Load and concatenate `file_type` across the given gameweeks.

    Missing files are skipped with a warning; empty (header-only) frames are
    skipped. Raises ValueError if nothing could be loaded.
    """
    frames = []
    for gw in gws:
        try:
            df = load_gw_csv(pl_dir, gw, file_type)
        except FileNotFoundError:
            print(f"Warning: {file_type}.csv not found for GW{gw}, skipping...")
            continue
        if df.empty:
            continue
        frames.append(df)

    if not frames:
        raise ValueError(f"No data loaded for {file_type} over gameweeks {gws}")

    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Rolling helpers (shift(1)-lagged, grouped per player)
# ---------------------------------------------------------------------------

def _rolling_mean(df: pd.DataFrame, col: str, window: int) -> pd.Series:
    """Shifted rolling mean grouped by player id (prevents target leakage)."""
    return (
        df.groupby("id")[col]
        .transform(lambda x: x.shift(1).rolling(window, min_periods=1).mean())
    )


def _rolling_std(df: pd.DataFrame, col: str, window: int) -> pd.Series:
    """Shifted rolling std grouped by player id."""
    return (
        df.groupby("id")[col]
        .transform(lambda x: x.shift(1).rolling(window, min_periods=2).std())
    )


# ---------------------------------------------------------------------------
# Master dataset assembly
# ---------------------------------------------------------------------------

def build_master_dataset(pl_dir: Path, gws) -> pd.DataFrame:
    """Assemble one row per player per gameweek.

    Joins player_gameweek_stats with position/team_code taken from each
    gameweek's own players.csv snapshot (per (player, gameweek), NOT the latest
    snapshot) so mid-season transfers are attributed to the correct club per
    gameweek, and per-match stats aggregated from playermatchstats.
    """
    df = load_range(pl_dir, gws, "player_gameweek_stats")

    # Position/team_code per gameweek from each GW's own players.csv snapshot.
    # Using one latest snapshot for all GWs would retroactively stamp
    # mid-season transfers with the new club for pre-transfer GWs and corrupt
    # team/opponent features; a per-(player, gameweek) join avoids that.
    info_frames = []
    for gw in gws:
        try:
            snap = load_gw_csv(pl_dir, gw, "players")
        except FileNotFoundError:
            continue
        info_frames.append(snap[["player_id", "gameweek", "position", "team_code"]])
    player_info = pd.concat(info_frames, ignore_index=True)
    player_info["position"] = player_info["position"].map(POSITION_MAP)
    df = df.merge(
        player_info, left_on=["id", "gameweek"],
        right_on=["player_id", "gameweek"], how="left",
    )

    # Per-match stats aggregated per (player_id, gameweek); handles double GWs.
    match_stats = load_range(pl_dir, gws, "playermatchstats")
    available_agg = {k: v for k, v in MATCH_AGG_COLS.items() if k in match_stats.columns}
    match_agg = match_stats.groupby(["player_id", "gameweek"]).agg(available_agg).reset_index()
    rename_map = {col: f"match_{col}" for col in available_agg if col != "player_id"}
    match_agg = match_agg.rename(columns=rename_map)

    df = df.merge(
        match_agg, left_on=["id", "gameweek"], right_on=["player_id", "gameweek"],
        how="left", suffixes=("", "_match"),
    )

    # Sort for the rolling operations.
    df = df.sort_values(["id", "gameweek"]).reset_index(drop=True)

    # Cover any coverage gaps (a player-GW stats row whose GW snapshot lacks
    # that player) by carrying position/team_code forward then backward within
    # each player. Guards against a snapshot omission silently NaN-ing features.
    for col in ["position", "team_code"]:
        df[col] = df.groupby("id")[col].transform(lambda s: s.ffill().bfill())

    return df


# ---------------------------------------------------------------------------
# Feature blocks
# ---------------------------------------------------------------------------

def add_rolling_features(df: pd.DataFrame, windows=(3, 5)) -> pd.DataFrame:
    df = df.copy()
    rolling_cols = {
        "event_points": "points",
        "match_xg": "xg",
        "match_xa": "xa",
        "bps": "bps",
        "ict_index": "ict",
        "minutes": "minutes",
        "defensive_contribution": "defcon",
    }
    for source_col, feature_name in rolling_cols.items():
        if source_col not in df.columns:
            continue
        for w in windows:
            df[f"{feature_name}_roll{w}"] = _rolling_mean(df, source_col, w)
    return df


def add_consistency_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["points_std5"] = _rolling_std(df, "event_points", 5)
    return df


def build_team_rolling_stats(pl_dir: Path, gws) -> pd.DataFrame:
    """Build rolling team-level stats from matches.csv.

    Reshapes each match into two team-perspective rows, aggregates per
    (team_code, gameweek) (sum xG, mean ELO for double GWs), then applies a
    shift(1) 5-GW rolling mean for xG for/against. ELO from matches.csv is
    already the PRE-match rating (leakage-free for its own GW), so it is used
    unshifted.
    """
    matches = load_range(pl_dir, gws, "matches")

    home_rows = matches.rename(columns={
        "home_team": "team_code",
        "home_expected_goals_xg": "xg_for",
        "away_expected_goals_xg": "xg_against",
        "home_team_elo": "team_elo",
    })[["team_code", "gameweek", "xg_for", "xg_against", "team_elo"]]

    away_rows = matches.rename(columns={
        "away_team": "team_code",
        "away_expected_goals_xg": "xg_for",
        "home_expected_goals_xg": "xg_against",
        "away_team_elo": "team_elo",
    })[["team_code", "gameweek", "xg_for", "xg_against", "team_elo"]]

    team_matches = pd.concat([home_rows, away_rows], ignore_index=True)
    team_matches["team_code"] = team_matches["team_code"].astype(int)

    agg_dict = {"xg_for": "sum", "xg_against": "sum", "team_elo": "mean"}
    team_gw = team_matches.groupby(["team_code", "gameweek"]).agg(agg_dict).reset_index()
    team_gw = team_gw.sort_values(["team_code", "gameweek"]).reset_index(drop=True)

    for col in ["xg_for", "xg_against"]:
        team_gw[f"team_{col}_roll5"] = (
            team_gw.groupby("team_code")[col]
            .transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
        )

    # team_elo is left unshifted: the aggregated per-GW value is already the
    # pre-match Elo entering that gameweek's fixture(s).

    result_cols = ["team_code", "gameweek",
                   "team_xg_for_roll5", "team_xg_against_roll5", "team_elo"]
    return team_gw[result_cols]


def add_team_features(df: pd.DataFrame, team_stats: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    own_team_cols = ["team_code", "gameweek",
                     "team_xg_for_roll5", "team_xg_against_roll5", "team_elo"]
    df["team_code"] = df["team_code"].astype(int)
    df = df.merge(team_stats[own_team_cols], on=["team_code", "gameweek"], how="left")
    return df


def add_opponent_features(df: pd.DataFrame, team_stats: pd.DataFrame,
                          pl_dir: Path, all_gws) -> pd.DataFrame:
    """Attach each player's same-GW opponent rolling stats.

    Builds a (team_code, gameweek) -> opponent list map from fixtures, then for
    every player row averages the opponent(s)' rolling stats (double GWs average
    across both opponents). NaN when there is no fixture or no stats row.
    """
    df = df.copy()

    all_fixtures = []
    for gw in all_gws:
        try:
            all_fixtures.append(load_gw_csv(pl_dir, gw, "fixtures"))
        except FileNotFoundError:
            continue
    fixtures = pd.concat(all_fixtures, ignore_index=True)

    team_fixture_map = {}
    for _, row in fixtures.iterrows():
        gw = int(row["gameweek"])
        home = int(row["home_team"])
        away = int(row["away_team"])
        team_fixture_map.setdefault((home, gw), []).append(away)
        team_fixture_map.setdefault((away, gw), []).append(home)

    opp_stat_cols = ["team_xg_against_roll5", "team_elo"]
    stats_lookup = {}
    for _, row in team_stats.iterrows():
        key = (int(row["team_code"]), int(row["gameweek"]))
        stats_lookup[key] = {col: row[col] for col in opp_stat_cols}

    opp_results = {f'opp_{col.replace("team_", "")}': [] for col in opp_stat_cols}
    has_double = []

    for _, row in df.iterrows():
        tc = row.get("team_code")
        gw = row.get("gameweek")

        if pd.isna(tc) or pd.isna(gw):
            for key in opp_results:
                opp_results[key].append(np.nan)
            has_double.append(False)
            continue

        tc = int(tc)
        gw = int(gw)
        opponents = team_fixture_map.get((tc, gw), [])

        if not opponents:
            for key in opp_results:
                opp_results[key].append(np.nan)
            has_double.append(False)
            continue

        has_double.append(len(opponents) >= 2)

        opp_stats_list = [stats_lookup.get((opp, gw)) for opp in opponents]
        opp_stats_list = [s for s in opp_stats_list if s is not None]

        if not opp_stats_list:
            for key in opp_results:
                opp_results[key].append(np.nan)
            continue

        for col in opp_stat_cols:
            opp_key = f'opp_{col.replace("team_", "")}'
            vals = [s[col] for s in opp_stats_list if not pd.isna(s[col])]
            opp_results[opp_key].append(np.mean(vals) if vals else np.nan)

    for col_name, values in opp_results.items():
        df[col_name] = values
    df["has_double_gw"] = has_double
    return df


def add_value_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # now_cost is already in £M in this dataset.
    df["price"] = df["now_cost"]
    if "points_roll5" in df.columns:
        df["ppm_roll5"] = df["points_roll5"] / df["price"].replace(0, np.nan)
    return df


def add_position_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    gkp_mask = df["position"] == "GKP"
    if gkp_mask.any():
        gkp_df = df[gkp_mask]
        if "saves" in df.columns:
            df.loc[gkp_mask, "gkp_saves_roll3"] = _rolling_mean(gkp_df, "saves", 3).values

    def_mask = df["position"] == "DEF"
    if def_mask.any():
        def_df = df[def_mask]
        if "clean_sheets" in df.columns:
            df.loc[def_mask, "def_clean_sheet_roll5"] = _rolling_mean(def_df, "clean_sheets", 5).values
        def_df = def_df.copy()
        def_df["_attacking_returns"] = def_df["goals_scored"].fillna(0) + def_df["assists"].fillna(0)
        df.loc[def_mask, "def_attacking_return_rate"] = (
            def_df.groupby("id")["_attacking_returns"]
            .transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
            .values
        )

    mid_mask = df["position"] == "MID"
    if mid_mask.any():
        mid_df = df[mid_mask]
        if "creativity" in df.columns:
            df.loc[mid_mask, "mid_creativity_roll3"] = _rolling_mean(mid_df, "creativity", 3).values
        if "threat" in df.columns:
            df.loc[mid_mask, "mid_threat_roll3"] = _rolling_mean(mid_df, "threat", 3).values
        if "match_shots_on_target" in df.columns:
            df.loc[mid_mask, "mid_shots_on_target_roll3"] = _rolling_mean(mid_df, "match_shots_on_target", 3).values
        if "match_touches_opposition_box" in df.columns:
            df.loc[mid_mask, "mid_touches_box_roll3"] = _rolling_mean(mid_df, "match_touches_opposition_box", 3).values

    fwd_mask = df["position"] == "FWD"
    if fwd_mask.any():
        fwd_df = df[fwd_mask]
        if "match_xg" in df.columns:
            df.loc[fwd_mask, "fwd_xg_roll3"] = _rolling_mean(fwd_df, "match_xg", 3).values
        if "match_shots_on_target" in df.columns:
            df.loc[fwd_mask, "fwd_shots_on_target_roll3"] = _rolling_mean(fwd_df, "match_shots_on_target", 3).values
        if "match_touches_opposition_box" in df.columns:
            df.loc[fwd_mask, "fwd_touches_box_roll3"] = _rolling_mean(fwd_df, "match_touches_opposition_box", 3).values

    return df


def add_availability_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Only start_rate_roll5 is exported; `started` is an intermediate column.
    df["started"] = (df["starts"] == 1).astype(int)
    df["start_rate_roll5"] = _rolling_mean(df, "started", 5)
    return df


def add_setpiece_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    order_cols = {
        "penalties_order": "is_penalty_taker",
        "corners_and_indirect_freekicks_order": "is_corner_freekick_taker",
        "direct_freekicks_order": "is_direct_freekick_taker",
    }
    for source_col, feature_name in order_cols.items():
        if source_col not in df.columns:
            continue
        is_primary = (df[source_col] == 1).astype(int)
        df[feature_name] = (
            is_primary.groupby(df["id"]).shift(1).fillna(0).astype(int)
        )
    return df


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def compute_features(pl_dir: Path, start_gw: int, end_gw: int) -> pd.DataFrame:
    """Run the full feature pipeline and return the combined feature frame.

    Features are ALWAYS computed over the full available history (GW1..latest)
    so that rolling windows are correctly seeded; ``start_gw``/``end_gw`` only
    filter which gameweeks' rows are returned (and therefore written). This is
    why e.g. ``--start-gw 38 --end-gw 38`` still yields fully-populated rolling
    features for GW38 rather than an all-NaN sheet.

    The returned frame contains the OUTPUT_COLUMNS (keys + features), one row
    per player per gameweek over [start_gw, end_gw].
    """
    all_gws = list_gameweeks(pl_dir)
    if not all_gws:
        raise ValueError(f"No gameweek folders found under {pl_dir}")

    df = build_master_dataset(pl_dir, all_gws)
    df = add_rolling_features(df)
    df = add_consistency_features(df)

    team_stats = build_team_rolling_stats(pl_dir, all_gws)
    df = add_team_features(df, team_stats)
    df = add_opponent_features(df, team_stats, pl_dir, all_gws)

    df = add_value_features(df)
    df = add_position_features(df)
    df = add_availability_features(df)
    df = add_setpiece_features(df)

    # Export key: player_id is the source player id.
    df["player_id"] = df["id"]

    # Ensure every expected output column exists (fill absent ones with NaN).
    for col in OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan

    out = df[OUTPUT_COLUMNS].copy()

    # Range flags are output filters only — features were computed full-history.
    out = out[(out["gameweek"] >= start_gw) & (out["gameweek"] <= end_gw)].copy()
    return out


def write_per_gameweek(features: pd.DataFrame, pl_dir: Path) -> int:
    """Write a `calculated_features.csv` into each gameweek folder present.

    Returns the number of files written.
    """
    written = 0
    for gw, gw_df in features.groupby("gameweek"):
        out_path = pl_dir / f"GW{int(gw)}" / "calculated_features.csv"
        # Serialize floats to 6 significant figures at write time only — the
        # in-memory frame keeps full precision so the parity test still compares
        # exact computations. This just shrinks noisy float diffs on disk.
        gw_df.to_csv(out_path, index=False, float_format="%.6g")
        print(f"   GW{int(gw)}: {len(gw_df)} rows -> {out_path}")
        written += 1
    return written


def _season_has_played_data(pl_dir: Path) -> bool:
    """True if any Premier League GW folder contains recorded player match stats.

    A freshly-rolled-over season ships GW folders with player snapshots and
    scheduled (unplayed) fixtures, but header-only `playermatchstats.csv` (no
    games played yet). Such a season cannot produce features, so it is not a
    valid default target.
    """
    for gw in list_gameweeks(pl_dir):
        stats_path = pl_dir / f"GW{gw}" / "playermatchstats.csv"
        if not stats_path.exists():
            continue
        try:
            if len(pd.read_csv(stats_path)) > 0:
                return True
        except pd.errors.EmptyDataError:
            continue
    return False


def find_default_season(data_dir: Path):
    """Return the latest `data/<season>` dir with played Premier League data.

    Seasons are considered newest-first; a preseason season with GW folders but
    no played matches is skipped so the default always targets real data.
    """
    for d in sorted(data_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        pl_dir = d / "By Tournament" / "Premier League"
        if pl_dir.is_dir() and list_gameweeks(pl_dir) and _season_has_played_data(pl_dir):
            return d.name
    return None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Build the Premier League calculated_features sheet.",
    )
    parser.add_argument(
        "--season", default=None,
        help="Season directory under data/ (e.g. 2025-2026). "
             "Defaults to the latest season with Premier League GW folders.",
    )
    parser.add_argument(
        "--start-gw", type=int, default=None,
        help="First gameweek file to (re)write (default: 1). Output filter only; "
             "features are always computed over the full GW1..latest history.",
    )
    parser.add_argument(
        "--end-gw", type=int, default=None,
        help="Last gameweek file to (re)write (default: latest). Output filter only; "
             "features are always computed over the full GW1..latest history.",
    )
    parser.add_argument(
        "--data-dir", default="data",
        help="Root data directory (default: data, i.e. run from the repo root).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    data_dir = Path(args.data_dir)

    season = args.season or find_default_season(data_dir)
    if season is None:
        print(f"Error: no season with Premier League GW folders found under {data_dir}/")
        return 1

    pl_dir = data_dir / season / "By Tournament" / "Premier League"
    if not pl_dir.is_dir():
        print(f"Error: Premier League data not found at {pl_dir}")
        return 1

    all_gws = list_gameweeks(pl_dir)
    if not all_gws:
        print(f"Error: no GW folders under {pl_dir}")
        return 1

    start_gw = args.start_gw or 1
    end_gw = args.end_gw or max(all_gws)

    print(f"Season: {season}")
    print(f"Premier League dir: {pl_dir}")
    print(f"Gameweek range: GW{start_gw}-GW{end_gw}")

    print("Computing calculated features...")
    features = compute_features(pl_dir, start_gw, end_gw)
    print(f"Computed {len(features)} feature rows across {features['gameweek'].nunique()} gameweeks.")

    print("Writing per-gameweek files...")
    written = write_per_gameweek(features, pl_dir)
    print(f"Done. Wrote {written} calculated_features.csv files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
