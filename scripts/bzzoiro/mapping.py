"""Field mapping between the Bzzoiro Sports Data API and this repo's schema.

Derived from the Bzzoiro OpenAPI 3 spec (v1.0.0, `PlayerStat` / `Event`
schemas). Nothing here talks to the network; it is the static translation
layer the probe uses to report coverage.
"""

# --- Repo schema (data/{season}/**/playermatchstats.csv) -------------------

REPO_PMS_COLUMNS = [
    "player_id", "match_id", "minutes_played", "goals", "assists",
    "total_shots", "xg", "xa", "shots_on_target", "successful_dribbles",
    "big_chances_missed", "touches_opposition_box", "touches",
    "accurate_passes", "accurate_passes_percent", "chances_created",
    "final_third_passes", "accurate_crosses", "accurate_crosses_percent",
    "accurate_long_balls", "accurate_long_balls_percent", "tackles_won",
    "interceptions", "recoveries", "blocks", "clearances",
    "headed_clearances", "dribbled_past", "duels_won", "duels_lost",
    "ground_duels_won", "ground_duels_won_percent", "aerial_duels_won",
    "aerial_duels_won_percent", "was_fouled", "fouls_committed", "saves",
    "goals_conceded", "xgot_faced", "goals_prevented", "sweeper_actions",
    "gk_accurate_passes", "gk_accurate_long_balls", "dispossessed",
    "high_claim", "corners", "saves_inside_box", "offsides",
    "successful_dribbles_percent", "tackles_won_percent", "xgot", "tackles",
    "start_min", "finish_min", "team_goals_conceded", "penalties_scored",
    "penalties_missed", "top_speed", "distance_covered", "walking_distance",
    "running_distance", "sprinting_distance", "number_of_sprints",
    "defensive_contributions",
]

# --- Direct field mappings: bzzoiro PlayerStat -> repo playermatchstats ----

PMS_DIRECT = {
    "minutes_played": "minutes_played",
    "goals": "goals",
    "goal_assist": "assists",
    "total_shots": "total_shots",
    "shots_on_target": "shots_on_target",
    "expected_goals": "xg",
    "expected_assists": "xa",
    "dribble_won": "successful_dribbles",
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
    "duel_won": "ground_duels_won",
    "aerial_won": "aerial_duels_won",
    "was_fouled": "was_fouled",
    "fouls": "fouls_committed",
    "saves": "saves",
    "goals_conceded": "goals_conceded",
    "penalty_miss": "penalties_missed",
    "dispossessed": "dispossessed",
}

# Repo columns computable from a pair of bzzoiro fields (numerator/denominator).
PMS_DERIVED = {
    "successful_dribbles_percent": ("dribble_won", "dribble_attempted"),
    "tackles_won_percent": ("won_tackle", "total_tackle"),
    "accurate_passes_percent": ("accurate_pass", "total_pass"),
    "accurate_crosses_percent": ("accurate_cross", "total_cross"),
    "accurate_long_balls_percent": ("accurate_long_balls", "total_long_balls"),
    "ground_duels_won_percent": ("duel_won", "duel_lost"),   # won/(won+lost)
    "aerial_duels_won_percent": ("aerial_won", "aerial_lost"),
    "duels_won": ("duel_won", "aerial_won"),                 # sum, not ratio
    "duels_lost": ("duel_lost", "aerial_lost"),              # sum, not ratio
}

# Repo columns with no bzzoiro equivalent. `blocks` is the costly one: FPL's
# defensive contribution needs CBIT (def) / CBIRT (mid+fwd), and blocks is a
# term in both, so DefCon cannot be reconstructed from this source alone.
PMS_UNAVAILABLE = [
    "blocks", "headed_clearances", "dribbled_past", "defensive_contributions",
    "xgot", "xgot_faced", "goals_prevented", "sweeper_actions", "high_claim",
    "saves_inside_box", "gk_accurate_passes", "gk_accurate_long_balls",
    "big_chances_missed", "touches_opposition_box", "final_third_passes",
    "corners", "offsides", "penalties_scored", "start_min", "finish_min",
    "team_goals_conceded", "top_speed", "distance_covered",
    "walking_distance", "running_distance", "sprinting_distance",
    "number_of_sprints",
]

# Bzzoiro PlayerStat fields with no repo column — candidates to add.
PMS_NEW_FROM_BZZOIRO = {
    "rating": "Match rating (1.0-10.0)",
    "heatmap": "Per-player touch coordinates, {x, y} on a 0-100 pitch",
    "possession_lost": "Times possession lost (bad touch or pass)",
    "penalty_won": "Penalties won",
    "penalty_conceded": "Penalties conceded (fouls in the box)",
    "penalty_faced": "Penalties faced (goalkeeper only)",
    "penalty_save": "Penalties saved (goalkeeper only)",
    "yellow_card": "Yellow cards (repo carries cards only at season level)",
    "red_card": "Red cards (repo carries cards only at season level)",
    "duel_lost": "Ground duels lost",
    "aerial_lost": "Aerial duels lost",
    "dribble_attempted": "Dribbles attempted",
    "total_pass": "Total passes attempted",
    "total_cross": "Total crosses attempted",
    "total_long_balls": "Total long balls attempted",
}

# --- Event-level data the repo has no table for at all --------------------

EVENT_NEW_TABLES = {
    "shotmap": (
        "Per-shot {min, type, sit, body, home, xg, xgot, pos:{x,y}, "
        "gm:{y,z}, gml, pid}. Requires ?full=true. No repo equivalent."
    ),
    "xg_per_minute": "Per-minute xG buckets aggregated from the shotmap.",
    "momentum": "Minute-by-minute pressure index {m, v}.",
    "average_positions": "Average pitch position per player {player, pid, pos, number}.",
    "lineups": "Confirmed XI ~1h pre-KO, or AI-predicted XI with confidence score.",
    "unavailable_players": "Injured / suspended / doubtful, grouped by team.",
    "incidents": "Goals, cards, substitutions with minute and player.",
    "referee": "Referee with yellow/red card rates.",
    "weather": "temperature_c, wind_speed, weather_code, pitch_condition.",
    "travel_distance_km": "Distance between team home cities.",
    "sr_stats": "ball_safe / attack / dangerous_attack counters.",
}

# Team-level: bzzoiro TeamV2Schema exposes `elo`, overlapping teams.csv:elo
# (currently sourced from ClubElo). Worth cross-checking, not blindly swapping.
TEAM_OVERLAP = {"elo": "teams.csv:elo (currently ClubElo)"}


# --- Team name aliases ----------------------------------------------------
#
# teams.csv carries FPL's abbreviated names ("Man Utd", "Spurs", "Nott'm
# Forest") and its `fotmob_name` column is empty for 2026/27, so there is no
# full-name field to match on. Generic normalisation is actively wrong here:
# stripping "city"/"utd" collapses Man City and Man Utd onto the same token.
# An explicit table is the only thing that holds up.
#
# Covers both 2025/26 and 2026/27 squads (relegated clubs included, since the
# last-season probe needs them).
TEAM_ALIASES = {
    "Arsenal": ["arsenal"],
    "Aston Villa": ["aston villa", "villa"],
    "Bournemouth": ["bournemouth", "afc bournemouth"],
    "Brentford": ["brentford"],
    "Brighton": ["brighton", "brighton and hove albion", "brighton & hove albion",
                 "brighton hove albion"],
    "Burnley": ["burnley"],
    "Chelsea": ["chelsea"],
    "Coventry City": ["coventry", "coventry city"],
    "Crystal Palace": ["crystal palace", "palace"],
    "Everton": ["everton"],
    "Fulham": ["fulham"],
    "Hull City": ["hull", "hull city"],
    "Ipswich Town": ["ipswich", "ipswich town"],
    "Leeds": ["leeds", "leeds united"],
    "Leicester": ["leicester", "leicester city"],
    "Liverpool": ["liverpool"],
    "Man City": ["manchester city", "man city"],
    "Man Utd": ["manchester united", "man united", "man utd", "manchester utd"],
    "Newcastle": ["newcastle", "newcastle united"],
    "Nott'm Forest": ["nottingham forest", "nottm forest", "nott'm forest",
                      "nottingham"],
    "Southampton": ["southampton"],
    "Spurs": ["tottenham hotspur", "tottenham", "spurs"],
    "Sunderland": ["sunderland"],
    "West Ham": ["west ham", "west ham united"],
    "Wolves": ["wolves", "wolverhampton", "wolverhampton wanderers"],
}

def _norm_name(s: str) -> str:
    """Fold a club name to a comparison key.

    Both the alias table and incoming provider names go through this, so
    punctuation differences ("Nott'm" vs "Nottm") cannot desync the two.
    """
    import re as _re
    import unicodedata as _ud

    if not s:
        return ""
    s = _ud.normalize("NFKD", str(s))
    s = "".join(c for c in s if not _ud.combining(c)).lower()
    s = _re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _re.sub(r"\b(fc|afc|cf|sc|ac|club)\b", " ", s)
    return _re.sub(r"\s+", " ", s).strip()


# Every alias, plus the FPL name itself, keyed by normalised form.
ALIAS_TO_FPL_NAME = {}
for _fpl, _aliases in TEAM_ALIASES.items():
    for _a in list(_aliases) + [_fpl]:
        ALIAS_TO_FPL_NAME.setdefault(_norm_name(_a), _fpl)

# Longest first: "manchester united" must win before "manchester" can grab it.
_ALIASES_BY_LENGTH = sorted(ALIAS_TO_FPL_NAME, key=len, reverse=True)


def resolve_team_name(provider_name: str) -> str | None:
    """Map a provider team name onto the FPL name used in teams.csv.

    Exact alias match first, then a containment fallback for decorated names
    like "Manchester City FC" or "Tottenham Hotspur F.C.".
    """
    s = _norm_name(provider_name)
    if not s:
        return None
    if s in ALIAS_TO_FPL_NAME:
        return ALIAS_TO_FPL_NAME[s]
    s_tokens = len(s.split())
    for alias in _ALIASES_BY_LENGTH:
        # A one-word alias ("hull", "brighton") may only match a one-word
        # candidate. Without this, "Hull Kingston Rovers" resolves to Hull City.
        if len(alias.split()) == 1 and s_tokens > 1:
            continue
        # Word-boundary containment, so "leeds" cannot match inside a longer word.
        if f" {alias} " in f" {s} " or f" {s} " in f" {alias} ":
            return ALIAS_TO_FPL_NAME[alias]
    return None


def coverage_summary():
    """Counts used by the probe report."""
    direct = set(PMS_DIRECT.values())
    derived = set(PMS_DERIVED)
    covered = direct | derived
    return {
        "repo_columns": len(REPO_PMS_COLUMNS),
        "direct": len(direct),
        "derived": len(derived),
        "covered": len(covered),
        "unavailable": len(PMS_UNAVAILABLE),
        "new_fields": len(PMS_NEW_FROM_BZZOIRO),
    }
