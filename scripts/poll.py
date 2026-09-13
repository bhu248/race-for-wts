#!/usr/bin/env python3
"""
One poll: fetch this week's live matchup scores + pre-game projections for
league LEAGUE_ID, and append one timestamped snapshot to data/week<N>.jsonl.

Meant to be run every few minutes by the GitHub Actions workflow during game
windows. Safe to run any time — outside the season it just prints a message
and exits without writing anything.
"""

import os
import sys

import common

LEAGUE_ID = os.environ.get("LEAGUE_ID", "1393377829990727680")


def compute_projected_total(starters, players_points, projections_by_player, scoring, players_team, team_progress):
    """
    Live-projected final total for one roster: for each starter, their
    actual points so far PLUS whatever's left of their pre-game projection
    above that, decayed by how far their own NFL game has progressed — so
    the "upside" on top of actual fades out smoothly and lands on exactly
    their actual total once their game ends, instead of staying pinned at
    a ceiling or cratering the instant they score.

        elapsed = team_progress[team] if actual != 0 else 0.0
        remaining = max(pregame_projection - actual, 0) * (1 - elapsed)
        contribution = actual + remaining

    where raw `elapsed` (0.0-1.0) comes from common.team_game_progress
    (ESPN's public scoreboard for that player's team's game clock), blended
    with common.estimate_scoring_fallback_progress for any team ESPN is
    still reporting as not-yet-started despite its players already posting
    real stats — see that function's docstring. Either way, `elapsed` only
    ever applies once the player has actually recorded a real stat (any
    NONZERO actual, positive or negative). A player sitting at exactly 0
    actual keeps their full, undecayed pre-game projection no matter how
    much wall-clock time has passed — "hasn't scored yet" is not evidence
    their opportunity is used up; it's just as likely their own game
    hasn't gotten to them yet. Without this gate, a quiet player's number
    would visibly crater over time for no reason connected to anything
    that actually happened in their game.

    FIXED 2026-09-11: the gate used to be `actual > 0`, not `actual != 0`.
    A team DEF/ST slot can legitimately have NEGATIVE actual points (this
    league's `pts_allow` is a per-point penalty, so a defense that gets
    torched nets negative) even once its game is completely over. `> 0`
    treated that exactly like "hasn't played yet," pinning a finished,
    leaky defense at its full pregame projection instead of its real
    (negative) final score — confirmed in production Week 1 data, where a
    roster's projected total sat ~7-8 points too high after its DEF's game
    went final with a negative actual score.

    FIXED 2026-09-10 (again): the first version of this decay applied
    `elapsed` to every starter unconditionally, including ones sitting at
    0 actual points. In production this showed up as EVERY roster's
    projected total sliding downward together, scorers and non-scorers
    alike, any time enough wall-clock time passed — the exact same
    complaint ("projections cratering for all teams") the kickoff-cliff
    bug produced two fixes ago, just reintroduced through a different
    mechanism (time instead of any-stat-at-all).

    FIXED 2026-09-10: replaced the previous max(actual, pregame_projection)
    approximation. That version kept a player pinned at their full
    pre-game projection for their ENTIRE game and only let the number move
    once actual overtook it — which was a real improvement over the
    original kickoff-cliff bug, but still wasn't a "live" projection, it
    was just a flatter static one. Sleeper's public API has no live,
    per-player projection feed at all (confirmed: the projections endpoint
    returns one unchanging pre-game number for the whole week — verified
    against this project's own real Week 1 data, where a roster with zero
    players who'd recorded any stat held an exactly unchanged projected
    total for over an hour of live play). This function is what actually
    makes the number live: it fakes the decay ourselves using each game's
    real clock instead of Sleeper's scoring data.

    If a player's team's game progress can't be determined (ESPN fetch
    failed, bye week, not yet posted), `team_progress.get(...)` returns
    None and we fall back to elapsed=0 — i.e. still show their full
    pre-game upside rather than silently re-creating the original
    kickoff-cliff bug by assuming their game is over.
    """
    actual_total = 0.0
    projected_total = 0.0
    for pid in starters:
        if pid == "0":  # empty slot
            continue
        pts = players_points.get(pid, 0.0) or 0.0
        actual_total += pts
        stats = projections_by_player.get(pid)
        pregame_projection = common.score_stats(stats, scoring) if stats else 0.0

        # Only decay once this player has actually recorded a real stat —
        # otherwise there's nothing connecting the clock to them
        # specifically, and we'd just be cratering a quiet player's number
        # for no in-game reason. This must be `!= 0.0`, not `> 0.0`: a
        # team DEF/ST slot can legitimately have NEGATIVE actual points
        # (e.g. this league's `pts_allow` penalty) despite its game being
        # fully over, and `> 0.0` was treating that exactly like "hasn't
        # played yet" — pinning a finished, leaky defense at its full
        # pregame projection instead of its real (negative) final score.
        if pts != 0.0:
            # A team defense/special-teams slot is keyed by the team's own
            # abbreviation (e.g. "SEA") rather than a normal player id.
            team = players_team.get(pid, pid)
            elapsed = team_progress.get(team)
            if elapsed is None:
                elapsed = 0.0
            elapsed = max(0.0, min(1.0, elapsed))
        else:
            elapsed = 0.0

        remaining = max(pregame_projection - pts, 0.0) * (1.0 - elapsed)
        projected_total += pts + remaining
    return round(actual_total, 2), round(projected_total, 2)


def main():
    state = common.get_state()
    season = state["season"]
    week = state.get("display_week") or state["week"]
    season_type = state["season_type"]

    if season_type not in ("regular", "post"):
        print(f"season_type={season_type!r}, not in-season — skipping poll.")
        return

    league = common.get_league(LEAGUE_ID)
    scoring = league.get("scoring_settings", {})

    matchups = common.get_matchups(LEAGUE_ID, week)
    projections_by_player = common.get_projections(season, week, season_type)
    players_team = common.load_player_teams()
    team_progress = common.team_game_progress(season, week, season_type)
    now_ts = common.now_iso()
    team_progress = common.estimate_scoring_fallback_progress(
        team_progress, matchups, players_team, common.load_snapshots(week), now_ts
    )

    rosters_out = {}
    for m in matchups:
        roster_id = str(m["roster_id"])
        starters = m.get("starters") or []
        players_points = m.get("players_points") or {}
        actual, projected = compute_projected_total(
            starters, players_points, projections_by_player, scoring, players_team, team_progress
        )
        rosters_out[roster_id] = {
            "actual": actual,
            "projected": projected,
            "players_points": {pid: players_points.get(pid, 0.0) for pid in starters if pid != "0"},
        }

    snapshot = {"ts": now_ts, "week": week, "rosters": rosters_out}
    common.append_snapshot(week, snapshot)
    print(f"[{snapshot['ts']}] week {week}: logged {len(rosters_out)} rosters.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - a bad poll should not fail the workflow run
        print(f"poll.py error (non-fatal, will retry next scheduled run): {exc}", file=sys.stderr)
        sys.exit(0)
