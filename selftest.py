"""
Local dry run with mocked network calls, to sanity check poll.py / render.py
logic before shipping. Not part of the delivered repo.
"""
import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))

import common  # noqa: E402

TEST_LEAGUE = "test123"

FAKE_STATE = {"season": "2026", "week": 1, "display_week": 1, "season_type": "regular"}

FAKE_LEAGUE = {
    "scoring_settings": {
        "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -1.0,
        "rush_yd": 0.1, "rush_td": 6.0,
        "rec": 0.5, "rec_yd": 0.1, "rec_td": 6.0,
        "fum_lost": -0.5, "fgm": 3.0, "fgmiss": -1.0, "xpm": 1.0, "xpmiss": -1.0,
    }
}

FAKE_USERS = [
    {"user_id": "u1", "display_name": "alice", "metadata": {"team_name": "Alpha Team"}},
    {"user_id": "u2", "display_name": "bob", "metadata": {}},
]
FAKE_ROSTERS = [
    {"roster_id": 1, "owner_id": "u1"},
    {"roster_id": 2, "owner_id": "u2"},
]

# poll #1: player p1 hasn't scored yet (0 actual -> use projection), p2 has 6 actual pts already
FAKE_MATCHUPS_1 = [
    {"roster_id": 1, "points": 6.0, "starters": ["p1", "p2"], "players_points": {"p1": 0.0, "p2": 6.0}},
    {"roster_id": 2, "points": 0.0, "starters": ["p3"], "players_points": {"p3": 0.0}},
]
# poll #2: p1 finally scores (a TD), p2 stays flat, p3 still scoreless
FAKE_MATCHUPS_2 = [
    {"roster_id": 1, "points": 12.6, "starters": ["p1", "p2"], "players_points": {"p1": 6.6, "p2": 6.0}},
    {"roster_id": 2, "points": 0.0, "starters": ["p3"], "players_points": {"p3": 0.0}},
]

FAKE_PROJECTIONS = {
    "p1": {"rush_yd": 40.0, "rush_td": 0.5},   # 40*0.1 + 0.5*6 = 7.0 projected
    "p2": {"rec": 4.0, "rec_yd": 50.0},         # 4*0.5 + 50*0.1 = 7.0 projected
    "p3": {"pass_yd": 250.0, "pass_td": 1.5},   # 10 + 6 = 16.0 projected
}

FAKE_PLAYERS_CACHE = {"p1": "Fake Runner (RB)", "p2": "Fake Catcher (WR)", "p3": "Fake Thrower (QB)"}
FAKE_PLAYER_TEAMS = {"p1": "AAA", "p2": "BBB", "p3": "CCC"}

# poll #1: no ESPN game-clock data yet (simulates pregame, or an ESPN
# hiccup) -> every team falls back to elapsed=0, so this should reduce to
# exactly the old max(actual, pregame_projection) behavior.
FAKE_PROGRESS_1 = {}
# poll #2: p1's team (AAA) is at halftime, p2's team (BBB) has finished —
# each player's "upside" above their actual should decay accordingly. p3's
# team (CCC) is ALSO well into its game (0.9) even though p3 personally is
# still scoreless -- this is the case that must NOT decay: a quiet player
# isn't evidence their opportunity is gone, just that it hasn't arrived yet.
FAKE_PROGRESS_2 = {"AAA": 0.5, "BBB": 1.0, "CCC": 0.9}


def install_mocks():
    common.get_state = lambda: FAKE_STATE
    common.get_league = lambda league_id: FAKE_LEAGUE
    common.get_users = lambda league_id: FAKE_USERS
    common.get_rosters = lambda league_id: FAKE_ROSTERS
    common.get_projections = lambda season, week, season_type="regular": FAKE_PROJECTIONS
    common.load_players_cache = lambda: FAKE_PLAYERS_CACHE
    common.load_player_teams = lambda: FAKE_PLAYER_TEAMS
    common._matchup_seq = iter([FAKE_MATCHUPS_1, FAKE_MATCHUPS_2])
    common.get_matchups = lambda league_id, week: next(common._matchup_seq)
    common._progress_seq = iter([FAKE_PROGRESS_1, FAKE_PROGRESS_2])
    common.team_game_progress = lambda season, week, season_type="regular": next(common._progress_seq)


def test_scoring_fallback():
    """
    Regression test for the ESPN-dormant scoring fallback added 2026-09-10:
    ESPN can leave a game marked STATUS_SCHEDULED (team_game_progress
    returning {}) for hours after its real kickoff, even while that team's
    players are already posting real stats. Decay must still kick in for a
    team that's clearly playing, estimated from when its own players first
    scored -- while a genuinely scoreless team must stay pinned at its full
    pregame projection throughout, exactly like the ESPN-driven case above.
    """
    sandbox = tempfile.mkdtemp(prefix="scoreboard-selftest-fallback-")
    data_dir = os.path.join(sandbox, "data")
    os.makedirs(data_dir)

    fake_state = {"season": "2026", "week": 1, "display_week": 1, "season_type": "regular"}
    fake_matchups = [
        {"roster_id": 1, "points": 2.0, "starters": ["q1"], "players_points": {"q1": 2.0}},
        {"roster_id": 2, "points": 0.0, "starters": ["q2"], "players_points": {"q2": 0.0}},
    ]
    fake_projections = {
        "q1": {"rush_yd": 40.0, "rush_td": 0.5},  # 7.0 projected
        "q2": {"rec": 4.0, "rec_yd": 50.0},        # 7.0 projected
    }
    fake_player_teams = {"q1": "AAA", "q2": "BBB"}
    now_seq = iter([
        "2026-09-09T20:00:00Z",  # poll 1: q1's first-ever score, right now -> elapsed=0
        "2026-09-09T21:45:00Z",  # poll 2: +1h45m -> half the 3h30m fallback duration -> elapsed=0.5
        "2026-09-09T23:30:00Z",  # poll 3: +3h30m from poll 1 -> elapsed=1.0 (fully decayed)
    ])

    common.get_state = lambda: fake_state
    common.get_league = lambda league_id: FAKE_LEAGUE
    common.get_matchups = lambda league_id, week: fake_matchups
    common.get_projections = lambda season, week, season_type="regular": fake_projections
    common.load_player_teams = lambda: fake_player_teams
    common.team_game_progress = lambda season, week, season_type="regular": {}  # ESPN permanently dormant
    common.now_iso = lambda: next(now_seq)
    common.DATA_DIR = data_dir
    common.PLAYERS_CACHE = os.path.join(data_dir, "players_cache.json")
    os.environ["LEAGUE_ID"] = TEST_LEAGUE

    import poll
    poll.LEAGUE_ID = TEST_LEAGUE
    poll.main()
    poll.main()
    poll.main()

    snaps = common.load_snapshots(1)
    assert len(snaps) == 3, f"expected 3 snapshots, got {len(snaps)}"

    q1_1, q1_2, q1_3 = (s["rosters"]["1"] for s in snaps)
    assert q1_1["actual"] == 2.0 and q1_1["projected"] == 7.0, q1_1
    assert q1_2["actual"] == 2.0 and q1_2["projected"] == 4.5, q1_2
    assert q1_3["actual"] == 2.0 and q1_3["projected"] == 2.0, q1_3

    for q2_snap in (s["rosters"]["2"] for s in snaps):
        assert q2_snap["actual"] == 0.0 and q2_snap["projected"] == 7.0, q2_snap

    print("ESPN-dormant scoring fallback: PASS")
    print("  poll 1 (elapsed=0.0):", q1_1)
    print("  poll 2 (elapsed=0.5):", q1_2)
    print("  poll 3 (elapsed=1.0):", q1_3)

    shutil.rmtree(sandbox)


def test_negative_actual_decay():
    """
    Regression test for the negative-actual gate bug fixed 2026-09-11:
    compute_projected_total used to gate decay on `actual > 0`, which
    treated a team DEF/ST slot with a NEGATIVE actual (this league's
    `pts_allow` penalty) as if it hadn't played yet, pinning it at its
    full pregame projection even after its game went final. Confirmed in
    production Week 1 data (LAR@SF): Team Mehta's Rams DEF had actual
    -2.9 with the game fully over, but projected still showed the full
    +4.78 pregame projection, inflating the roster's total by ~7.7 points.

    A player/team with a nonzero actual (whether positive OR negative)
    must decay toward that actual as their game progresses, exactly like
    a positive-scoring player would.
    """
    sandbox = tempfile.mkdtemp(prefix="scoreboard-selftest-negative-")
    data_dir = os.path.join(sandbox, "data")
    os.makedirs(data_dir)

    fake_state = {"season": "2026", "week": 1, "display_week": 1, "season_type": "regular"}
    # The Rams DEF gave up enough points that its actual is negative, even
    # though its game is fully over (elapsed=1.0). Keyed by the team's own
    # abbreviation "LAR", exactly like real Sleeper matchup data (a DEF/ST
    # starter slot has no separate numeric player id — see
    # common.load_player_teams's docstring) and NOT present in
    # fake_player_teams below, so players_team.get("LAR", "LAR") correctly
    # falls back to itself instead of accidentally testing a normal player.
    fake_matchups = [
        {"roster_id": 1, "points": -2.9, "starters": ["LAR"], "players_points": {"LAR": -2.9}},
    ]
    # pregame projection is a normal POSITIVE 4.8 (3.0 pts allowed * -0.2,
    # plus a projected sack worth 1.0 each) -- the real final actual (-2.9)
    # came in worse than projected, which is exactly the case the old
    # `actual > 0` gate got wrong: it fell back to elapsed=0 and left the
    # roster pinned at the full +4.8 instead of decaying to the real -2.9.
    fake_projections = {
        "LAR": {"pts_allow": 3.0, "sack": 5.4},  # 3.0*-0.2 + 5.4*1.0 = 4.8
    }
    fake_league = {"scoring_settings": {"pts_allow": -0.2, "sack": 1.0}}
    fake_player_teams = {}

    common.get_state = lambda: fake_state
    common.get_league = lambda league_id: fake_league
    common.get_matchups = lambda league_id, week: fake_matchups
    common.get_projections = lambda season, week, season_type="regular": fake_projections
    common.load_player_teams = lambda: fake_player_teams
    common.team_game_progress = lambda season, week, season_type="regular": {"LAR": 1.0}  # game is final
    common.now_iso = lambda: "2026-09-11T03:35:00Z"
    common.DATA_DIR = data_dir
    common.PLAYERS_CACHE = os.path.join(data_dir, "players_cache.json")
    os.environ["LEAGUE_ID"] = TEST_LEAGUE

    import poll
    poll.LEAGUE_ID = TEST_LEAGUE
    poll.main()

    snaps = common.load_snapshots(1)
    assert len(snaps) == 1, f"expected 1 snapshot, got {len(snaps)}"
    r1 = snaps[0]["rosters"]["1"]
    # Game is final (elapsed=1.0) -> projected must equal the real negative
    # actual, NOT the full +4.8 pregame projection the old `actual > 0`
    # gate would have produced.
    assert r1["actual"] == -2.9, r1
    assert r1["projected"] == -2.9, r1

    print("negative-actual (leaky DEF) decay: PASS")
    print("  final snapshot:", r1)

    shutil.rmtree(sandbox)


def test_stale_read_floor():
    """
    Regression test for the stale-read floor added 2026-09-13: Sleeper's
    live matchup endpoint occasionally serves a transiently stale read (a
    lagging replica during high-traffic windows) where a player's points
    briefly drop below what an earlier poll already recorded, then
    self-correct a poll or two later. Confirmed in production: player 9221
    (roster 14) went 12.3 -> 6.2 -> 12.3 across three consecutive polls,
    with six other players across six other rosters dropping the same
    minute — and because the flash logic only fires on point *increases*,
    that poll's frame showed zero flashes, which is what first surfaced
    this.

    A normal player's actual points must never be recorded lower than a
    prior poll already saw this week. A DEF/ST slot (players_team.get
    falls back to the pid itself for those) must be EXEMPT from this floor
    — this league's pts_allow penalty legitimately gets MORE negative as a
    defense gives up more points, so it has to be allowed to move in
    either direction, same reasoning as the negative-actual gate in
    compute_projected_total.
    """
    sandbox = tempfile.mkdtemp(prefix="scoreboard-selftest-staleread-")
    data_dir = os.path.join(sandbox, "data")
    os.makedirs(data_dir)

    fake_state = {"season": "2026", "week": 1, "display_week": 1, "season_type": "regular"}
    # poll #1: p1 (normal player) at 10.0, def1 (DEF slot) at -1.0.
    fake_matchups_1 = [
        {"roster_id": 1, "points": 10.0, "starters": ["p1"], "players_points": {"p1": 10.0}},
        {"roster_id": 2, "points": -1.0, "starters": ["def1"], "players_points": {"def1": -1.0}},
    ]
    # poll #2: p1's raw read regresses to 6.0 (the stale-replica glitch) —
    # must be floored back to 10.0. def1 legitimately worsens to -3.0 (gave
    # up more points) — must NOT be floored, since that's real, not stale.
    fake_matchups_2 = [
        {"roster_id": 1, "points": 6.0, "starters": ["p1"], "players_points": {"p1": 6.0}},
        {"roster_id": 2, "points": -3.0, "starters": ["def1"], "players_points": {"def1": -3.0}},
    ]
    fake_projections = {}
    fake_player_teams = {"p1": "AAA"}  # def1 deliberately absent -> resolves to itself, i.e. a DEF slot
    matchup_seq = iter([fake_matchups_1, fake_matchups_2])

    common.get_state = lambda: fake_state
    common.get_league = lambda league_id: FAKE_LEAGUE
    common.get_matchups = lambda league_id, week: next(matchup_seq)
    common.get_projections = lambda season, week, season_type="regular": fake_projections
    common.load_player_teams = lambda: fake_player_teams
    common.team_game_progress = lambda season, week, season_type="regular": {}
    common.now_iso = lambda: "2026-09-13T17:33:00Z"
    common.DATA_DIR = data_dir
    common.PLAYERS_CACHE = os.path.join(data_dir, "players_cache.json")
    os.environ["LEAGUE_ID"] = TEST_LEAGUE

    import poll
    poll.LEAGUE_ID = TEST_LEAGUE
    poll.main()
    poll.main()

    snaps = common.load_snapshots(1)
    assert len(snaps) == 2, f"expected 2 snapshots, got {len(snaps)}"
    r1_poll2 = snaps[1]["rosters"]["1"]
    r2_poll2 = snaps[1]["rosters"]["2"]

    assert r1_poll2["players_points"]["p1"] == 10.0, r1_poll2  # floored, not the stale 6.0
    assert r1_poll2["actual"] == 10.0, r1_poll2
    assert r2_poll2["players_points"]["def1"] == -3.0, r2_poll2  # DEF exempt, real worsening kept
    assert r2_poll2["actual"] == -3.0, r2_poll2

    print("stale-read floor (non-DEF only): PASS")
    print("  poll 2, roster 1 (floored p1):", r1_poll2)
    print("  poll 2, roster 2 (unfloored DEF):", r2_poll2)

    shutil.rmtree(sandbox)


def main():
    # Run entirely inside a throwaway temp directory — this must NEVER touch
    # the real data/ and docs/ folders in a cloned repo, since those hold
    # real season snapshots and the published pages once the season starts.
    sandbox = tempfile.mkdtemp(prefix="scoreboard-selftest-")
    data_dir = os.path.join(sandbox, "data")
    docs_dir = os.path.join(sandbox, "docs")
    os.makedirs(data_dir)
    os.makedirs(docs_dir)

    install_mocks()
    common.DATA_DIR = data_dir
    common.PLAYERS_CACHE = os.path.join(data_dir, "players_cache.json")
    os.environ["LEAGUE_ID"] = TEST_LEAGUE

    import poll
    poll.LEAGUE_ID = TEST_LEAGUE
    poll.main()
    poll.main()  # second poll -> second snapshot

    snaps = common.load_snapshots(1)
    assert len(snaps) == 2, f"expected 2 snapshots, got {len(snaps)}"
    r1 = snaps[0]["rosters"]["1"]
    assert r1["actual"] == 6.0, r1
    # no ESPN game-clock data yet -> elapsed=0 for every team -> reduces to
    # the old max(actual, pregame_projection): max(0,7)=7 for p1, max(6,7)=7 for p2
    assert r1["projected"] == 14.0, r1
    r2 = snaps[1]["rosters"]["1"]
    assert r2["actual"] == 12.6, r2
    # p1 (team AAA, at halftime/elapsed=0.5): actual 6.6 + (7-6.6)*0.5 = 6.8
    # p2 (team BBB, game over/elapsed=1.0):   actual 6.0 + (7-6.0)*0.0 = 6.0
    # 6.8 + 6.0 = 12.8 -- the live decay actually moves the number now,
    # instead of staying frozen at 14.0 all game like the old pinned model.
    assert r2["projected"] == 12.8, r2

    # p3 (roster 2) never scores in either poll, even though by poll #2
    # their team's game is 90% elapsed -- projected must stay pinned at the
    # full 16.0 pre-game projection both times, NOT decay toward 0 just
    # because time passed. This is the exact bug just fixed: every roster's
    # projected total was cratering together regardless of whether that
    # roster had scored anything.
    p3_poll1 = snaps[0]["rosters"]["2"]
    p3_poll2 = snaps[1]["rosters"]["2"]
    assert p3_poll1["actual"] == 0.0 and p3_poll1["projected"] == 16.0, p3_poll1
    assert p3_poll2["actual"] == 0.0 and p3_poll2["projected"] == 16.0, p3_poll2

    print("poll.py logic: PASS")
    print("  snapshot 1, roster 1:", r1)
    print("  snapshot 2, roster 1:", r2)
    print("  snapshot 2, roster 2 (scoreless all game):", p3_poll2)

    import render
    render.LEAGUE_ID = TEST_LEAGUE
    render.DOCS_DIR = docs_dir
    out = render.render_week(1)
    assert out and os.path.exists(out)
    with open(out) as f:
        html = f.read()
    assert "Alpha Team" in html
    assert "Fake Runner (RB)" in html  # flash label for p1's jump between poll 1 and poll 2
    print("render.py logic: PASS —", out, f"({len(html)} bytes)")

    render.render_index()
    idx = os.path.join(docs_dir, "index.html")
    assert os.path.exists(idx)
    with open(idx) as f:
        assert "week1.html" in f.read()
    print("index render: PASS")

    shutil.rmtree(sandbox)

    test_scoring_fallback()
    test_negative_actual_decay()
    test_stale_read_floor()

    print("\nALL SELFTESTS PASSED (ran entirely in a throwaway temp dir — your real data/ and docs/ were untouched)")


if __name__ == "__main__":
    main()
