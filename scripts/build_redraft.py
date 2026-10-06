"""
Builds docs/redraft.html, the "Draft Do-Over" simulator: a mock-draft where
the user re-drafts with perfect hindsight against 13 bots that draft badly.

Run this by hand (not part of the polling workflow):

    python scripts/build_redraft.py               # through the current week
    python scripts/build_redraft.py --through 4   # pin a specific week

It bakes everything the page needs into one JSON blob inside the HTML
(no runtime API calls from the browser), same approach as render.py:

- The "ADP board" is this league's own real 2026 preseason draft, pick by
  pick. A full-depth public preseason ADP list no longer exists by now —
  FantasyFootballCalculator's API only serves its most recent ~54-player
  window, and Sleeper's redraft ADP has already moved with the season (it
  would leak hindsight into the bots). Players nobody drafted are appended
  after pick 224, ordered by Sleeper's frozen week-1 projection.
- Weekly points are scored with the league's own scoring_settings via
  common.score_stats, the same scorer the live scoreboard uses.
- Each team's real weekly points and opponents come from the league's
  matchups, so the page can replay the super team against real schedules.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

LEAGUE_ID = "1393377829990727680"
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
UNDRAFTED_BY_POINTS = 120   # undrafted players kept in the pool, by points scored
UNDRAFTED_BY_PROJ = 60      # ...plus this many by preseason projection, so the bots have sane late picks

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "scripts", "redraft_template.html")
OUT = os.path.join(ROOT, "docs", "redraft.html")

LABEL_RE = re.compile(r"^(.*) \(([A-Z]+)\)$")


def week_is_final(season, week):
    """True once ESPN reports every game of that week as final."""
    try:
        events = common.get_scoreboard(season, week).get("events", [])
    except Exception:
        return False
    names = [((e.get("status") or {}).get("type") or {}).get("name") for e in events]
    return bool(names) and all(n in ("STATUS_FINAL", "STATUS_FULL_TIME", "STATUS_POSTPONED", "STATUS_CANCELED") for n in names)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--through", type=int, help="last week to include (default: current week)")
    args = ap.parse_args()

    state = common.get_state()
    season = str(state["season"])
    through = args.through or int(state.get("display_week") or state["week"])
    weeks = list(range(1, through + 1))

    league = common.get_league(LEAGUE_ID)
    scoring = league["scoring_settings"]
    names = common.team_names(LEAGUE_ID)
    rosters = common.get_rosters(LEAGUE_ID)
    owner_to_roster = {r["owner_id"]: r["roster_id"] for r in rosters if r.get("owner_id")}

    draft = common._get(f"{common.BASE}/draft/{league['draft_id']}")
    picks = common._get(f"{common.BASE}/draft/{league['draft_id']}/picks")
    num_teams = int(draft["settings"]["teams"])
    rounds = int(draft["settings"]["rounds"])
    slot_by_roster = {owner_to_roster[u]: s for u, s in draft["draft_order"].items() if u in owner_to_roster}

    labels = common.load_players_cache()
    teams_of = common.load_player_teams()

    def ident(pid):
        m = LABEL_RE.match(labels.get(pid, ""))
        if not m:
            return None, None
        return m.group(1), m.group(2)

    # --- Points per player per week, league scoring ---
    weekly = {}  # pid -> [pts per week]
    for i, w in enumerate(weeks):
        stats = common._get(f"{common.BASE}/stats/nfl/regular/{season}/{w}")
        for pid, st in stats.items():
            if not isinstance(st, dict):
                continue
            pts = common.score_stats(st, scoring)
            if pts or st.get("gp"):
                weekly.setdefault(pid, [0.0] * len(weeks))[i] = pts

    # Preseason projection, used only to order undrafted players on the board.
    proj = {pid: common.score_stats(st, scoring) for pid, st in common.get_projections(season, 1).items()}

    # --- Real draft = the ADP board ---
    drafted = {}
    real_picks = {}  # roster_id -> [pid by round]
    for p in sorted(picks, key=lambda x: x["pick_no"]):
        rid = owner_to_roster.get(p["picked_by"]) or p["roster_id"]
        drafted[p["player_id"]] = {"adp": p["pick_no"], "rid": rid, "rd": p["round"]}
        real_picks.setdefault(rid, []).append(p["player_id"])

    def pos_ok(pid):
        return ident(pid)[1] in POSITIONS

    undrafted = [pid for pid in set(weekly) | set(proj) if pid not in drafted and pos_ok(pid)]
    by_pts = sorted(undrafted, key=lambda pid: -sum(weekly.get(pid, [0])))[:UNDRAFTED_BY_POINTS]
    by_proj = sorted(undrafted, key=lambda pid: -proj.get(pid, 0))[:UNDRAFTED_BY_PROJ]
    pool_undrafted = sorted(set(by_pts) | set(by_proj), key=lambda pid: -proj.get(pid, 0))

    players = []
    for pid in list(drafted) + pool_undrafted:
        name, pos = ident(pid)
        if not name or pos not in POSITIONS:
            continue
        w = [round(x, 2) for x in weekly.get(pid, [0.0] * len(weeks))]
        d = drafted.get(pid)
        players.append({
            "id": pid, "n": name, "p": pos, "t": teams_of.get(pid, pid if pos == "DEF" else ""),
            "adp": d["adp"] if d else None, "by": d["rid"] if d else None,
            "w": w, "tot": round(sum(w), 2),
        })
    for i, p in enumerate(x for x in players if x["adp"] is None):
        p["adp"] = len(picks) + 1 + i

    # Position rank by total points.
    for pos in POSITIONS:
        ranked = sorted((p for p in players if p["p"] == pos), key=lambda p: -p["tot"])
        for r, p in enumerate(ranked, 1):
            p["pr"] = r

    # --- Real season so far: each roster's weekly points and opponents ---
    teams = {
        r["roster_id"]: {"rid": r["roster_id"], "name": names[str(r["roster_id"])],
                         "slot": slot_by_roster.get(r["roster_id"]),
                         "picks": real_picks.get(r["roster_id"], []), "wk": []}
        for r in rosters
    }
    for w in weeks:
        ms = common.get_matchups(LEAGUE_ID, w)
        by_mid = {}
        for m in ms:
            by_mid.setdefault(m.get("matchup_id"), []).append(m)
        for m in ms:
            opp = next((o for o in by_mid.get(m.get("matchup_id"), []) if o["roster_id"] != m["roster_id"]), None)
            teams[m["roster_id"]]["wk"].append({
                "pts": round(m.get("points") or 0, 2),
                "opp": opp["roster_id"] if opp else None,
                "opp_pts": round(opp.get("points") or 0, 2) if opp else None,
            })

    data = {
        "season": season,
        "weeks": weeks,
        "partial": not week_is_final(season, through),
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "num_teams": num_teams,
        "rounds": rounds,
        "lineup": [s for s in league["roster_positions"] if s != "BN"],
        "teams": sorted(teams.values(), key=lambda t: t["slot"] or 99),
        "players": sorted(players, key=lambda p: p["adp"]),
    }

    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace("/*__REDRAFT_DATA__*/null", blob)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {OUT}: {len(players)} players, weeks {weeks[0]}-{weeks[-1]}"
          f"{' (week not final yet)' if data['partial'] else ''}.")


if __name__ == "__main__":
    main()
