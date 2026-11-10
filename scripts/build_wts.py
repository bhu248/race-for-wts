"""
Builds docs/wts.html, the "Make a WTS Team" game: each round deals a random
manager's roster from one week, the user picks one player from it into an
open lineup slot, and at the end the picks' real fantasy points are tallied.

Run this by hand (not part of the polling workflow):

    python scripts/build_wts.py            # the most recent week Sleeper is displaying
    python scripts/build_wts.py --week 9   # pin a specific week

Everything the page needs is baked into one JSON blob inside the HTML (no
runtime API calls from the browser), same approach as build_redraft.py.
Points are Sleeper's own league-scored players_points from that week's
matchups, so they match what the league's scoreboard showed.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from build_redraft import LEAGUE_ID, week_is_final  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "scripts", "wts_template.html")
OUT = os.path.join(ROOT, "docs", "wts.html")

LABEL_RE = re.compile(r"^(.*) \(([A-Z]+)\)$")
POS_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4, "DEF": 5}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, help="week to use (default: Sleeper's display week)")
    args = ap.parse_args()

    state = common.get_state()
    season = str(state["season"])
    week = args.week or int(state.get("display_week") or state["week"])

    league = common.get_league(LEAGUE_ID)
    names = common.team_names(LEAGUE_ID)
    labels = common.load_players_cache()
    teams_of = common.load_player_teams()

    teams = []
    for m in common.get_matchups(LEAGUE_ID, week):
        pts = m.get("players_points") or {}
        starters = set(m.get("starters") or [])
        players = []
        for pid in m.get("players") or []:
            lm = LABEL_RE.match(labels.get(pid, ""))
            if not lm or lm.group(2) not in POS_ORDER:
                continue
            name, pos = lm.groups()
            players.append({
                "n": name, "p": pos, "t": teams_of.get(pid, pid if pos == "DEF" else ""),
                "pts": round(pts.get(pid) or 0.0, 2), "s": pid in starters,
            })
        players.sort(key=lambda p: (POS_ORDER[p["p"]], not p["s"], p["n"]))
        teams.append({
            "name": names[str(m["roster_id"])],
            "pts": round(m.get("points") or 0.0, 2),
            "players": players,
        })

    data = {
        "season": season,
        "week": week,
        "partial": not week_is_final(season, week),
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "lineup": [s for s in league["roster_positions"] if s not in ("BN", "IR", "TAXI")],
        "teams": sorted(teams, key=lambda t: t["name"].lower()),
    }

    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace("/*__WTS_DATA__*/null", blob)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {OUT}: week {week}, {len(teams)} teams"
          f"{' (week not final yet)' if data['partial'] else ''}.")


if __name__ == "__main__":
    main()
