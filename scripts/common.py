"""
Shared helpers for talking to Sleeper's public API.

Everything here is a plain read-only GET — Sleeper's API needs no auth key.
Rate-limit guidance from https://docs.sleeper.com/ is "stay under 1000 calls/minute";
a single poll of one league uses ~4 calls, so we are nowhere near that.

The one endpoint Sleeper asks callers to go easy on is /players/nfl (a ~5MB
dump of every player). We cache it to disk and only re-fetch if the cache is
missing or more than 20 hours old.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

# A fixed stand-in for "how long an NFL game runs end-to-end," used only by
# estimate_scoring_fallback_progress() below when ESPN hasn't confirmed a
# game is live yet. Real games vary, but this is just meant to turn "some
# time has passed since this team clearly started scoring" into a rough
# decay curve -- not to be precise.
FALLBACK_GAME_DURATION_SEC = 3.5 * 3600

import requests

BASE = "https://api.sleeper.app/v1"
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
PLAYERS_CACHE = os.path.join(DATA_DIR, "players_cache.json")
PLAYERS_CACHE_MAX_AGE_SEC = 20 * 60 * 60  # 20 hours

# ESPN's public, unauthenticated scoreboard feed. This is the ONLY
# non-Sleeper external call in the project, and it exists solely to read
# each game's live clock (period + displayClock) so poll.py can tell how
# far into a player's own game it is — see team_game_progress() below for
# why. We never read scores or stats from this feed; Sleeper stays the
# sole source of truth for every point total.
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ESPN_SEASON_TYPE = {"pre": 1, "regular": 2, "post": 3}
ESPN_TEAM_ALIAS = {"WSH": "WAS"}  # ESPN's abbreviation -> Sleeper's, where they differ

_session = requests.Session()
_session.headers.update({"User-Agent": "race-for-wts/1.0 (+github actions)"})


def _get(url, params=None):
    resp = _session.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_state():
    """Current NFL season/week, so the poller never needs a week hardcoded."""
    return _get(f"{BASE}/state/nfl")


def get_league(league_id):
    return _get(f"{BASE}/league/{league_id}")


def get_rosters(league_id):
    return _get(f"{BASE}/league/{league_id}/rosters")


def get_users(league_id):
    return _get(f"{BASE}/league/{league_id}/users")


def get_matchups(league_id, week):
    return _get(f"{BASE}/league/{league_id}/matchups/{week}")


def get_projections(season, week, season_type="regular"):
    """
    Bulk pre-game projections for every player in a given week.
    Undocumented endpoint. The URL shape matters: season_type must be a PATH
    SEGMENT (.../nfl/{season_type}/{season}/{week}) — the query-parameter
    form (.../nfl/{season}/{week}?season_type=...) that an earlier version of
    this function used looks like it works (200 OK, valid JSON) but silently
    returns every player mapped to an empty {}, which would have made every
    live "projected" total during games just equal the actual total with no
    projection boost. Re-verified by direct fetch on 2026-09-08 against both
    a completed historical week (2025 wk1) and the live 2026 wk1 slate.

    The response is already a flat dict of player_id -> raw stat-category
    projections (yards, TDs, receptions...) — NOT a list of row objects and
    NOT a single point total. We score it ourselves with the league's own
    scoring_settings, since Sleeper's built-in pts_ppr/pts_half_ppr totals
    assume stock scoring and this league runs custom settings.
    """
    raw = _get(f"{BASE}/projections/nfl/{season_type}/{season}/{week}")
    by_player = {}
    for pid, stats in (raw or {}).items():
        if isinstance(stats, dict) and stats:
            by_player[pid] = stats
    return by_player


def _elapsed_fraction(period, display_clock, status_name):
    """
    0.0 (hasn't kicked off) to 1.0 (over) — how much of an NFL game's 4
    regulation quarters have elapsed, from ESPN's scoreboard status fields.
    Overtime (period 5+) is treated as fully elapsed: regular-season OT is
    a single short (10-minute) period, so by the time a game gets there its
    pre-game projection has essentially nothing meaningful left to give.
    """
    status_name = status_name or ""
    if status_name in ("STATUS_FINAL", "STATUS_FULL_TIME"):
        return 1.0
    if not period or period <= 0 or status_name in ("STATUS_SCHEDULED", "STATUS_POSTPONED", "STATUS_CANCELED"):
        return 0.0
    if period >= 5:
        return 1.0
    try:
        mins, secs = (display_clock or "0:00").split(":")
        remaining_sec = int(mins) * 60 + int(secs)
    except (ValueError, AttributeError):
        remaining_sec = 0
    quarter_sec = 15 * 60
    elapsed_sec = (period - 1) * quarter_sec + (quarter_sec - remaining_sec)
    return max(0.0, min(elapsed_sec / (4 * quarter_sec), 1.0))


def get_scoreboard(season, week, season_type="regular"):
    """Raw ESPN scoreboard response for one NFL week. See ESPN_SCOREBOARD_URL."""
    params = {
        "week": week,
        "seasontype": ESPN_SEASON_TYPE.get(season_type, 2),
        "year": season,
    }
    return _get(ESPN_SCOREBOARD_URL, params=params)


def team_game_progress(season, week, season_type="regular"):
    """
    NFL team abbreviation -> fraction (0.0-1.0) of THAT TEAM'S OWN game this
    week that has elapsed so far, e.g. {"SEA": 0.0, "KC": 0.62, ...}.

    Why this exists: Sleeper's public projections endpoint
    (get_projections, above) only ever returns one static pre-game number
    per player for the whole week — confirmed both by Sleeper's own docs
    (no live-projection endpoint is documented or exists) and by this
    project's real Week 1 production data: a roster whose players hadn't
    recorded a single stat held an EXACTLY unchanged projected total for
    over an hour of live play. There is no Sleeper feed of a live,
    per-player projection to fetch.

    So poll.py builds the "live" behavior itself: each player's remaining,
    not-yet-banked projection fades out over the course of their specific
    game, using ESPN's public scoreboard purely to read the clock. See
    poll.compute_projected_total for how this fraction gets used.

    Returns {} (every team treated as "not started") on any fetch/parse
    failure, so a flaky ESPN response degrades one poll's projected totals
    back to the old full-projection behavior rather than breaking the poll
    entirely or crashing the workflow.
    """
    try:
        data = get_scoreboard(season, week, season_type)
    except Exception as exc:  # noqa: BLE001 - a flaky third-party feed should never sink a poll
        print(f"team_game_progress: ESPN scoreboard fetch failed (non-fatal): {exc}", file=sys.stderr)
        return {}

    progress = {}
    for event in (data or {}).get("events", []):
        for comp in event.get("competitions") or []:
            status = comp.get("status") or {}
            status_type = status.get("type") or {}
            frac = _elapsed_fraction(
                status.get("period"), status.get("displayClock"), status_type.get("name"),
            )
            for competitor in comp.get("competitors") or []:
                abbr = ((competitor.get("team") or {}).get("abbreviation") or "").upper()
                if not abbr:
                    continue
                abbr = ESPN_TEAM_ALIAS.get(abbr, abbr)
                progress[abbr] = frac
    return progress


def count_live_games(season, week, season_type="regular"):
    """
    How many NFL games (across the WHOLE scoreboard, not just this league's
    rostered teams) are actually in progress right now -- used by
    local_scheduler.py to pick its polling cadence: a full slate of
    concurrent games justifies tighter polling, a single leftover game
    (the last one still going before or after the rest of a window) does
    not need to be checked as often.

    "In progress" excludes both ends: not yet kicked off
    (STATUS_SCHEDULED/POSTPONED/CANCELED) and already over
    (STATUS_FINAL/STATUS_FULL_TIME), matching the same status vocabulary
    _elapsed_fraction already relies on above. Returns 0 on any
    fetch/parse failure -- same fail-safe posture as team_game_progress,
    though callers should treat 0 here as "unknown," not "confirmed
    quiet," and default to the tighter cadence rather than under-polling.
    """
    try:
        data = get_scoreboard(season, week, season_type)
    except Exception as exc:  # noqa: BLE001 - a flaky third-party feed should never sink a poll
        print(f"count_live_games: ESPN scoreboard fetch failed (non-fatal): {exc}", file=sys.stderr)
        return 0

    not_live = ("STATUS_SCHEDULED", "STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_FINAL", "STATUS_FULL_TIME")
    count = 0
    for event in (data or {}).get("events", []):
        for comp in event.get("competitions") or []:
            status_name = ((comp.get("status") or {}).get("type") or {}).get("name")
            if status_name not in not_live:
                count += 1
    return count


def _parse_iso(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def estimate_scoring_fallback_progress(espn_progress, matchups, players_team, history_snapshots, now_ts_iso):
    """
    ESPN's scoreboard has been observed to leave a game marked
    STATUS_SCHEDULED for hours after its real kickoff (confirmed in this
    project's own Week 1 opener, which ESPN never once reported as anything
    but STATUS_SCHEDULED despite being live for over two hours). When that
    happens, team_game_progress() reports elapsed=0 for that team even
    though its players are already posting real stats in Sleeper's matchup
    data, so compute_projected_total's decay never engages and a team's
    projected total sits frozen at its pregame number all game.

    This fills that specific gap: for any team ESPN is NOT already
    reporting live progress for (frac <= 0, i.e. missing or STATUS_
    SCHEDULED), find the earliest snapshot -- in this week's full history,
    plus the in-progress poll that hasn't been saved yet -- where any of
    that team's players had a NONZERO actual (positive or negative -- a
    DEF/ST slot's `pts_allow` penalty can be negative from its very first
    scoring play, and that's just as much evidence of kickoff as a
    positive stat), and treat that moment as a proxy for kickoff:
    elapsed = (now - that moment) / FALLBACK_GAME_DURATION_SEC.

    ESPN's real clock is always trusted the moment it actually reports one
    (frac > 0) for a team -- this estimate only ever fills in for teams
    ESPN is still silently treating as not-yet-started. Because it's a
    fixed-duration guess rather than a real clock, there can be a one-time
    jump when ESPN's status finally catches up and takes back over -- an
    accepted tradeoff against leaving the number frozen for however long
    ESPN stays silent.
    """
    now = _parse_iso(now_ts_iso)
    first_score_ts = {}

    def fold_in(players_points, ts):
        for pid, pts in (players_points or {}).items():
            # `!= 0.0`, not `<= 0.0` / `not pts`: a team DEF/ST slot can
            # have a genuinely negative actual (e.g. a pts_allow penalty)
            # from its very first scoring play, and that's just as much
            # proof the game has started as a positive stat would be.
            if pts is None or pts == 0.0:
                continue
            team = players_team.get(pid, pid)
            if team not in first_score_ts or ts < first_score_ts[team]:
                first_score_ts[team] = ts

    for snap in history_snapshots:
        ts = _parse_iso(snap["ts"])
        for roster in snap.get("rosters", {}).values():
            fold_in(roster.get("players_points"), ts)
    for m in matchups:
        fold_in(m.get("players_points"), now)

    blended = dict(espn_progress)
    for team, first_ts in first_score_ts.items():
        if blended.get(team, 0.0) > 0.0:
            continue  # ESPN already has a real clock for this team -- trust it
        elapsed_sec = (now - first_ts).total_seconds()
        blended[team] = max(0.0, min(1.0, elapsed_sec / FALLBACK_GAME_DURATION_SEC))
    return blended


def score_stats(stats, scoring_settings):
    """
    Generic dot product: sum(stat_value * points_per_stat) over whatever
    categories are present in both. This is exactly how Sleeper itself scores
    a boxscore, so it works for any league's custom settings without us
    having to hardcode which categories matter.
    """
    total = 0.0
    for key, value in stats.items():
        weight = scoring_settings.get(key)
        if weight:
            total += value * weight
    return round(total, 2)


def team_names(league_id):
    """roster_id (str) -> display team name, falling back to the manager's username."""
    users_by_id = {u["user_id"]: u for u in get_users(league_id)}
    names = {}
    for roster in get_rosters(league_id):
        owner_id = roster.get("owner_id")
        user = users_by_id.get(owner_id, {})
        meta = user.get("metadata") or {}
        name = meta.get("team_name") or user.get("display_name") or f"Roster {roster['roster_id']}"
        names[str(roster["roster_id"])] = name
    return names


def _refresh_players_cache():
    raw = _get(f"{BASE}/players/nfl")
    labels = {}
    teams = {}
    for pid, p in raw.items():
        if not isinstance(p, dict):
            continue
        first = p.get("first_name") or ""
        last = p.get("last_name") or ""
        pos = p.get("position") or ""
        team = p.get("team") or ""
        label = (f"{first} {last}".strip() or pid)
        if pos:
            label += f" ({pos})"
        elif team:
            label += f" ({team})"
        labels[pid] = label
        if team:
            teams[pid] = team
    cache = {"fetched_at": time.time(), "players": labels, "teams": teams}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PLAYERS_CACHE, "w") as f:
        json.dump(cache, f)
    return cache


def _load_players_cache_raw():
    """
    Shared on-disk cache of Sleeper's ~5MB player dump, refreshed at most
    once a day per Sleeper's own guidance for this endpoint. Holds both the
    human-readable label used for flash text ("J. Smith (RB)") and each
    player's current team — the team is needed by poll.py to look up that
    team's live game progress (see team_game_progress) for projected-total
    blending.

    The staleness check is based on a timestamp stored *inside* the cache
    file, not the file's mtime — this cache gets committed to the repo, and
    `git checkout` stamps every file with the current time regardless of
    when it was actually written, so an mtime check would never see it as
    stale once it's under version control.

    A cache written before the "teams" field existed is treated as stale
    too, so it gets one forced refresh instead of silently running forever
    with no team data.
    """
    if os.path.exists(PLAYERS_CACHE):
        with open(PLAYERS_CACHE, "r") as f:
            cached = json.load(f)
        fetched_at = cached.get("fetched_at", 0)
        if "teams" in cached and time.time() - fetched_at < PLAYERS_CACHE_MAX_AGE_SEC:
            return cached
    return _refresh_players_cache()


def load_players_cache():
    """player_id -> "First Last (POS)". Used only to label flashes — never called on the hot polling path."""
    return _load_players_cache_raw()["players"]


def load_player_teams():
    """
    player_id -> current NFL team abbreviation, e.g. "SEA". A team
    defense/special-teams starter slot is keyed directly by the team's own
    abbreviation in Sleeper's matchup data (there's no separate "player" for
    a DST) — callers looking up a starter's team should do
    `players_team.get(pid, pid)` so that case just resolves to itself.
    """
    return _load_players_cache_raw()["teams"]


def week_data_path(week):
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, f"week{week}.jsonl")


def append_snapshot(week, snapshot):
    path = week_data_path(week)
    with open(path, "a") as f:
        f.write(json.dumps(snapshot, separators=(",", ":")) + "\n")


def load_snapshots(week):
    path = week_data_path(week)
    if not os.path.exists(path):
        return []
    snaps = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                snaps.append(json.loads(line))
    return snaps
