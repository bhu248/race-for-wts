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

    # remaining = 7.0 * (1-elapsed)**2, independent of actual (2026-09-13
    # change) -- so poll 1 (elapsed=0) is actual + the FULL pregame value,
    # not capped at pregame like the old max(pregame-actual,0) did.
    q1_1, q1_2, q1_3 = (s["rosters"]["1"] for s in snaps)
    assert q1_1["actual"] == 2.0 and q1_1["projected"] == 9.0, q1_1     # 2.0 + 7.0*(1-0.0)**2 = 9.0
    assert q1_2["actual"] == 2.0 and q1_2["projected"] == 3.75, q1_2    # 2.0 + 7.0*(1-0.5)**2 = 3.75
    assert q1_3["actual"] == 2.0 and q1_3["projected"] == 2.0, q1_3     # 2.0 + 7.0*(1-1.0)**2 = 2.0

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


def test_overperformer_upside():
    """
    Regression test for the 2026-09-13 remaining-upside formula change in
    compute_projected_total: `remaining` used to be
    `max(pregame_projection - actual, 0) * (1 - elapsed)`, which meant a
    player who'd already exceeded their FULL pregame projection got
    credited with ZERO further upside for the rest of their game, however
    much time was left. Confirmed in production (Team lynnbear, week 1):
    Trevor Lawrence, Derrick Henry, and Parker Washington had each already
    exceeded their full pregame projection with roughly half their game
    left, and the roster's projected total barely moved past its actual
    despite three starters clearly having big days.

    `remaining` is now `max(pregame_projection, 0) * (1 - elapsed) ** 2`
    -- independent of actual entirely, so a hot player keeps accruing
    projected upside past their pregame ceiling, decaying only with time
    remaining (squared, as an extra discount against trusting a small,
    possibly-noisy sample too much).
    """
    sandbox = tempfile.mkdtemp(prefix="scoreboard-selftest-overperform-")
    data_dir = os.path.join(sandbox, "data")
    os.makedirs(data_dir)

    fake_state = {"season": "2026", "week": 1, "display_week": 1, "season_type": "regular"}
    # hot's actual (18.34) already exceeds their full pregame projection
    # (17.35) with the game only half over (elapsed=0.5) -- like Trevor
    # Lawrence in the production case above. cold is a normal
    # still-on-pace player in the same game, included as a control to
    # confirm the formula change doesn't affect an unremarkable player.
    fake_matchups = [
        {"roster_id": 1, "points": 18.34, "starters": ["hot"], "players_points": {"hot": 18.34}},
        {"roster_id": 2, "points": 5.0, "starters": ["cold"], "players_points": {"cold": 5.0}},
    ]
    # A single trivial scoring stat, so pregame_projection == the raw
    # number below exactly -- keeps the assertions easy to hand-verify.
    fake_league = {"scoring_settings": {"stat": 1.0}}
    fake_projections = {
        "hot": {"stat": 17.35},
        "cold": {"stat": 10.0},
    }
    fake_player_teams = {"hot": "AAA", "cold": "AAA"}

    common.get_state = lambda: fake_state
    common.get_league = lambda league_id: fake_league
    common.get_matchups = lambda league_id, week: fake_matchups
    common.get_projections = lambda season, week, season_type="regular": fake_projections
    common.load_player_teams = lambda: fake_player_teams
    common.team_game_progress = lambda season, week, season_type="regular": {"AAA": 0.5}
    common.now_iso = lambda: "2026-09-13T18:00:00Z"
    common.DATA_DIR = data_dir
    common.PLAYERS_CACHE = os.path.join(data_dir, "players_cache.json")
    os.environ["LEAGUE_ID"] = TEST_LEAGUE

    import poll
    poll.LEAGUE_ID = TEST_LEAGUE
    poll.main()

    snaps = common.load_snapshots(1)
    hot = snaps[0]["rosters"]["1"]
    cold = snaps[0]["rosters"]["2"]

    # hot: 18.34 + 17.35*(1-0.5)**2 = 18.34 + 4.3375 = 22.6775 -> 22.68.
    # Must be strictly greater than actual -- the whole point is a hot
    # player with half a game left still projects for more, not a
    # flatline at their already-exceeded pregame ceiling (17.35).
    assert hot["actual"] == 18.34, hot
    assert hot["projected"] == 22.68, hot
    assert hot["projected"] > hot["actual"] > 17.35, hot

    # cold: 5.0 + 10.0*(1-0.5)**2 = 5.0 + 2.5 = 7.5 -- an ordinary
    # still-on-pace player, unaffected in kind by the formula change.
    assert cold["actual"] == 5.0, cold
    assert cold["projected"] == 7.5, cold

    print("overperformer keeps decaying upside past pregame ceiling: PASS")
    print("  hot (exceeded pregame mid-game):", hot)
    print("  cold (control, still on pace):", cold)

    shutil.rmtree(sandbox)


def test_scoreless_player_uses_known_team_progress():
    """
    Regression test for the 2026-09-13 gate change in compute_projected_
    total: elapsed used to only ever apply to a player once THEY personally
    recorded a nonzero stat, on the theory that a scoreless player's own
    game might just not have gotten to them yet. Confirmed in production
    that reasoning doesn't hold once we have a REAL, team-specific signal:
    ESPN reported Colston Loveland's Bears game at 98% elapsed while he
    personally stayed at 0 actual all game, but his projected total never
    moved off its full pregame number because the old gate refused to
    apply that 98% to him at all.

    Two scoreless players in this test, same pregame projection, to
    isolate the one thing that differs: quiet_a's team has a REAL known
    elapsed (0.8) -- he must decay just like a scorer would. quiet_b's
    team has NO progress signal at all (not "0", genuinely unknown) --
    he must stay pinned at the full pregame projection, exactly like
    bug #2 requires, since there's zero evidence his game has even
    started. The fix is not "always decay scoreless players" (that would
    be bug #2 again) -- it's "decay them exactly when their team's
    progress is actually known," decoupled from whether the gate is
    keyed on the player's own stats.
    """
    sandbox = tempfile.mkdtemp(prefix="scoreboard-selftest-scoreless-")
    data_dir = os.path.join(sandbox, "data")
    os.makedirs(data_dir)

    fake_state = {"season": "2026", "week": 1, "display_week": 1, "season_type": "regular"}
    fake_matchups = [
        {"roster_id": 1, "points": 0.0, "starters": ["quiet_a"], "players_points": {"quiet_a": 0.0}},
        {"roster_id": 2, "points": 0.0, "starters": ["quiet_b"], "players_points": {"quiet_b": 0.0}},
    ]
    fake_projections = {
        "quiet_a": {"stat": 20.0},
        "quiet_b": {"stat": 20.0},
    }
    fake_league = {"scoring_settings": {"stat": 1.0}}
    fake_player_teams = {"quiet_a": "AAA", "quiet_b": "BBB"}
    # AAA's real progress is known (0.8); BBB's is absent entirely, not 0 --
    # nobody on BBB (anywhere in the league) has scored and ESPN has
    # nothing for it either, i.e. genuinely no evidence it's even started.
    fake_progress = {"AAA": 0.8}

    common.get_state = lambda: fake_state
    common.get_league = lambda league_id: fake_league
    common.get_matchups = lambda league_id, week: fake_matchups
    common.get_projections = lambda season, week, season_type="regular": fake_projections
    common.load_player_teams = lambda: fake_player_teams
    common.team_game_progress = lambda season, week, season_type="regular": fake_progress
    common.now_iso = lambda: "2026-09-13T21:00:00Z"
    common.DATA_DIR = data_dir
    common.PLAYERS_CACHE = os.path.join(data_dir, "players_cache.json")
    os.environ["LEAGUE_ID"] = TEST_LEAGUE

    import poll
    poll.LEAGUE_ID = TEST_LEAGUE
    poll.main()

    snaps = common.load_snapshots(1)
    quiet_a = snaps[0]["rosters"]["1"]
    quiet_b = snaps[0]["rosters"]["2"]

    # quiet_a: 0.0 + 20.0*(1-0.8)**2 = 0.0 + 0.8 = 0.8 -- decays even
    # though he personally never scored, because his TEAM's progress is
    # known and nearly over.
    assert quiet_a["actual"] == 0.0, quiet_a
    assert quiet_a["projected"] == 0.8, quiet_a

    # quiet_b: no progress signal for BBB at all -> elapsed=0.0 -> stays
    # pinned at the full 20.0 pregame projection. Not a regression of
    # bug #2: this isn't decaying off generic wall-clock time, it's
    # correctly finding NO evidence BBB's game has started.
    assert quiet_b["actual"] == 0.0, quiet_b
    assert quiet_b["projected"] == 20.0, quiet_b

    print("scoreless player decays only when their OWN team's progress is known: PASS")
    print("  quiet_a (team progress known, 80% elapsed):", quiet_a)
    print("  quiet_b (team progress unknown):", quiet_b)

    shutil.rmtree(sandbox)


def test_regression_smoothing():
    """
    Regression test for the hindsight-based smoothing added 2026-09-13 in
    render.py's build_frames (NOT in poll.py -- see below for why not).

    Two confirmed production cases pull in opposite directions:

    1. Jahmyr Gibbs (roster 14, pid 9221) read 12.3 -> 6.2 -> 12.3 across
       three consecutive 3-minute polls on 2026-09-13 -- a transient stale
       read from a lagging Sleeper replica that self-corrected one poll
       later. This should be smoothed: the middle frame's dip is noise, not
       signal, and it suppressed that frame's pop-up flash entirely (the
       flash logic only fires on point *increases*).

    2. Blake Corum (roster 1, pid 11586) read ...6.8 -> 5.0... on
       2026-09-10 and never came back anywhere near 6.8 for the rest of
       that game or the rest of the week. Querying Sleeper's own
       long-since-final week 1 stats for him returns 5.4 -- i.e. 6.8 was
       itself the bad read (a spurious upward spike), not 5.0. This must
       NOT be smoothed: an earlier first attempt at this fix floored every
       drop at the running max forever, which would have permanently
       overstated this roster's score by ~1.8 points for the rest of the
       season -- a real fantasy scoreboard silently lying about who's
       winning is much worse than one frame missing a pop-up.

    The distinguishing signal is recovery speed, which a LIVE poll can't
    observe (it can't see "the next poll or two" before they happen) --
    this is why the fix lives in render.py, replayed over the full
    already-stored history every render, and never mutates
    data/week<N>.jsonl itself. A genuine stale read self-heals within
    STALE_READ_LOOKAHEAD snapshots of being recorded; a genuine correction
    doesn't recover at all.
    """
    sandbox = tempfile.mkdtemp(prefix="scoreboard-selftest-smoothing-")
    data_dir = os.path.join(sandbox, "data")
    docs_dir = os.path.join(sandbox, "docs")
    os.makedirs(data_dir)
    os.makedirs(docs_dir)

    fake_state = {"season": "2026", "week": 1, "display_week": 1, "season_type": "regular"}
    # roster 1 / g (Gibbs-style): dips one poll, recovers the next -> smooth.
    # roster 2 / c (Corum-style): dips and never recovers -> trust it.
    # roster 3 / def1 (DEF slot, absent from fake_player_teams so it
    # resolves to itself): dips like a DEF legitimately can -> never smoothed regardless.
    matchups_seq = [
        [
            {"roster_id": 1, "points": 12.3, "starters": ["g"], "players_points": {"g": 12.3}},
            {"roster_id": 2, "points": 6.8, "starters": ["c"], "players_points": {"c": 6.8}},
            {"roster_id": 3, "points": -1.0, "starters": ["def1"], "players_points": {"def1": -1.0}},
        ],
        [
            {"roster_id": 1, "points": 6.2, "starters": ["g"], "players_points": {"g": 6.2}},
            {"roster_id": 2, "points": 5.0, "starters": ["c"], "players_points": {"c": 5.0}},
            {"roster_id": 3, "points": -3.0, "starters": ["def1"], "players_points": {"def1": -3.0}},
        ],
        [
            {"roster_id": 1, "points": 12.3, "starters": ["g"], "players_points": {"g": 12.3}},
            {"roster_id": 2, "points": 5.0, "starters": ["c"], "players_points": {"c": 5.0}},
            {"roster_id": 3, "points": -3.0, "starters": ["def1"], "players_points": {"def1": -3.0}},
        ],
    ]
    fake_projections = {}
    fake_player_teams = {"g": "AAA", "c": "BBB"}  # def1 deliberately absent -> a DEF slot
    matchup_seq = iter(matchups_seq)
    now_seq = iter(["2026-09-13T17:30:00Z", "2026-09-13T17:33:00Z", "2026-09-13T17:36:00Z"])

    common.get_state = lambda: fake_state
    common.get_league = lambda league_id: FAKE_LEAGUE
    common.get_matchups = lambda league_id, week: next(matchup_seq)
    common.get_projections = lambda season, week, season_type="regular": fake_projections
    common.load_player_teams = lambda: fake_player_teams
    common.team_game_progress = lambda season, week, season_type="regular": {}
    common.now_iso = lambda: next(now_seq)
    common.DATA_DIR = data_dir
    common.PLAYERS_CACHE = os.path.join(data_dir, "players_cache.json")
    os.environ["LEAGUE_ID"] = TEST_LEAGUE

    import poll
    poll.LEAGUE_ID = TEST_LEAGUE
    poll.main()
    poll.main()
    poll.main()

    # data/week1.jsonl itself must be untouched -- the raw dips are stored exactly as Sleeper reported them.
    snaps = common.load_snapshots(1)
    assert [s["rosters"]["1"]["players_points"]["g"] for s in snaps] == [12.3, 6.2, 12.3]
    assert [s["rosters"]["2"]["players_points"]["c"] for s in snaps] == [6.8, 5.0, 5.0]

    import render
    frames = render.build_frames(snaps, ["1", "2", "3"], fake_player_teams)

    g_actuals = [f["teams"][0]["actual"] for f in frames]
    c_actuals = [f["teams"][1]["actual"] for f in frames]
    assert g_actuals == [12.3, 12.3, 12.3], g_actuals  # dip smoothed away
    assert c_actuals == [6.8, 5.0, 5.0], c_actuals      # sustained drop trusted, NOT floored back to 6.8

    # the dip frame must NOT flash (nothing really happened), and the
    # recovery frame must not either (it's a no-op once smoothed) --
    # otherwise smoothing would trade "missing flash" for "phantom flash."
    assert frames[1]["flashes"] == [], frames[1]["flashes"]
    assert frames[2]["flashes"] == [], frames[2]["flashes"]

    print("regression smoothing (render-time, non-destructive): PASS")
    print("  Gibbs-style (self-corrects) actuals:", g_actuals)
    print("  Corum-style (sustained drop) actuals:", c_actuals)

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
    # no ESPN game-clock data yet -> elapsed=0 for every team -> remaining
    # is each player's full pregame projection, undiscounted: p1 gets
    # 0 + 7*(1-0)**2 = 7, p2 gets 6 + 7*(1-0)**2 = 13 -- remaining no
    # longer subtracts actual first (2026-09-13 change), so p2 isn't
    # capped at their 7.0 pregame ceiling just because they've already
    # banked 6.0 of it.
    assert r1["projected"] == 20.0, r1
    r2 = snaps[1]["rosters"]["1"]
    assert r2["actual"] == 12.6, r2
    # p1 (team AAA, at halftime/elapsed=0.5): actual 6.6 + 7*(1-0.5)**2 = 6.6 + 1.75 = 8.35
    # p2 (team BBB, game over/elapsed=1.0):   actual 6.0 + 7*(1-1)**2   = 6.0 + 0    = 6.0
    # 8.35 + 6.0 = 14.35 -- the live decay actually moves the number now,
    # instead of staying frozen at 20.0 all game like the old pinned model.
    assert r2["projected"] == 14.35, r2

    # p3 (roster 2) never scores in either poll. Poll #1 has no progress
    # data for ANY team (FAKE_PROGRESS_1 = {}) -- with zero signal either
    # way, p3 must stay pinned at the full 16.0 pregame projection, NOT
    # decay toward 0 just because time passed. This is bug #2's fix: every
    # roster's projected total was cratering together off a generic
    # wall-clock timer, regardless of whether that roster had scored
    # anything or its team had even started.
    #
    # Poll #2 DOES have a real, team-specific signal for p3's team CCC
    # (FAKE_PROGRESS_2 sets it to 0.9 elapsed) -- unlike poll #1, this
    # isn't "no info," it's confirmed evidence CCC's game is almost over.
    # p3 must now decay same as a scoring player would (2026-09-13 change,
    # the Loveland fix): 16.0 * (1-0.9)**2 = 0.16. Staying pinned at the
    # full 16.0 here would repeat exactly the bug a user reported in
    # production -- Colston Loveland's Bears game reported 98% elapsed
    # while he personally stayed scoreless all game, but his projection
    # never moved off its full pregame number.
    p3_poll1 = snaps[0]["rosters"]["2"]
    p3_poll2 = snaps[1]["rosters"]["2"]
    assert p3_poll1["actual"] == 0.0 and p3_poll1["projected"] == 16.0, p3_poll1
    assert p3_poll2["actual"] == 0.0 and p3_poll2["projected"] == 0.16, p3_poll2

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
    test_overperformer_upside()
    test_scoreless_player_uses_known_team_progress()
    test_regression_smoothing()

    print("\nALL SELFTESTS PASSED (ran entirely in a throwaway temp dir — your real data/ and docs/ were untouched)")


if __name__ == "__main__":
    main()
