#!/usr/bin/env python3
"""
Turn data/week<N>.jsonl into docs/week<N>.html — the same time-lapse race
visual as the feasibility demo, but driven by real snapshots instead of a
scripted example. Also regenerates docs/index.html so there's one link that
always points at whatever weeks have been played.

Runs after every poll (cheap — it's just re-templating a small JSON blob),
so the page updates live through the day, not just once Monday night ends.
GitHub Pages serves whatever's in docs/ on main, so no separate deploy step
is needed beyond committing the file.
"""

import json
import os
import re
import sys

import common

LEAGUE_ID = os.environ.get("LEAGUE_ID", "1393377829990727680")
DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")

# The 8-hue validated categorical palette from the design system this project
# started from. With 14 teams we cycle it — every bar is already directly
# labeled with its team name, so a repeated hue is a cosmetic overlap, never
# an identity ambiguity.
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]


# How many snapshots ahead to look, when a player's points appear to have
# dropped, for a recovery back to (at least) the pre-drop level before
# accepting the drop as real. See _smooth_regressions's docstring.
STALE_READ_LOOKAHEAD = 2


def _smooth_regressions(raw_sequence):
    """
    Sleeper's live matchup endpoint occasionally serves a transiently stale
    read (a lagging replica during high-traffic windows) where a player's
    points briefly drop below a prior poll's value, then self-correct within
    a poll or two. Confirmed in production 2026-09-13: Jahmyr Gibbs (roster
    14) read 12.3 -> 6.2 -> 12.3 across three consecutive 3-minute polls.

    This must NOT be "fix" by simply flooring every drop at the running
    max, forever: also confirmed in production, Blake Corum (roster 1) read
    ...29.3 -> 31.2 -> 29.4... on 2026-09-10 and never came back anywhere
    near 31.2 for the rest of that game or the rest of the week -- querying
    Sleeper's own current (long-since-final) week 1 stats for him returns
    5.4 points, i.e. the LOWER 29.4-ish reading was closer to correct and
    31.2 was itself the bad read (a spurious upward spike), not the other
    way around. A naive "never decrease" floor would have permanently
    overstated that roster's score by ~1.8 points for the rest of the
    season. Real stat corrections are rare but real -- Sleeper does
    occasionally revise a play's yardage/TD after the fact -- and a
    sustained lower value must be trusted, not overridden.

    The distinguishing signal is recovery speed: a stale READ corrects
    itself within a poll or two; a genuine correction does not bounce back
    at all. So: given the full, already-recorded sequence for one
    (roster, player) in chronological order, only smooth a drop over if one
    of the next STALE_READ_LOOKAHEAD readings comes back up to at least the
    pre-drop level. A drop that never recovers within that window is
    accepted as the new real value, and comparisons going forward measure
    against it, not the old (apparently wrong) peak.

    This runs at render time, over the full already-stored history, and
    never writes anything back to data/week<N>.jsonl -- the stored file
    stays exactly what Sleeper reported. A live poll can't apply this (it
    can't see "the next poll or two" before they happen), so the newest
    frame in any given render may briefly show an unsmoothed dip until the
    next poll's data lets this function look ahead far enough to correct it
    retroactively -- exactly the ~3-6 minute self-heal window observed
    above.
    """
    trusted = None
    smoothed = []
    for i, v in enumerate(raw_sequence):
        if trusted is None or v >= trusted:
            trusted = v
        else:
            lookahead = raw_sequence[i + 1 : i + 1 + STALE_READ_LOOKAHEAD]
            if any(later >= trusted for later in lookahead):
                v = trusted  # transient dip: hold at the last trusted value
            else:
                trusted = v  # sustained drop: accept it as the new reality
        smoothed.append(v)
    return smoothed


def build_frames(snapshots, roster_ids, players_team):
    def is_def(pid):
        return players_team.get(pid, pid) == pid

    # Smooth each (roster, player)'s raw points across the whole history
    # before building any frame -- needs the full sequence up front, so it
    # can't be done incrementally frame-by-frame like the rest below.
    raw_by_rp = {}
    for snap in snapshots:
        for rid in roster_ids:
            r = snap.get("rosters", {}).get(rid) or {}
            for pid, pts in r.get("players_points", {}).items():
                raw_by_rp.setdefault((rid, pid), []).append(pts or 0.0)

    smoothed_by_rp = {
        key: seq if is_def(key[1]) else _smooth_regressions(seq)
        for key, seq in raw_by_rp.items()
    }
    next_idx = {key: 0 for key in raw_by_rp}

    frames = []
    prev_points = {rid: {} for rid in roster_ids}
    for snap in snapshots:
        rosters = snap.get("rosters", {})
        teams_frame = []
        flashes = []
        for rid in roster_ids:
            r = rosters.get(rid, {"actual": 0, "projected": 0, "players_points": {}})

            cur_pp = {}
            for pid in r.get("players_points", {}):
                key = (rid, pid)
                cur_pp[pid] = smoothed_by_rp[key][next_idx[key]]
                next_idx[key] += 1

            actual = round(sum(cur_pp.values()), 2) if cur_pp else r["actual"]
            projected = round(max(r["projected"], actual), 2)
            teams_frame.append({"id": rid, "actual": actual, "projected": projected})

            for pid, pts in cur_pp.items():
                delta = pts - prev_points[rid].get(pid, 0.0)
                if delta > 0.05:
                    flashes.append({"id": rid, "pid": pid, "delta": round(delta, 2)})
            prev_points[rid] = cur_pp

        frames.append({"ts": snap["ts"], "teams": teams_frame, "flashes": flashes})
    return frames


def render_week(week):
    snapshots = common.load_snapshots(week)
    names = common.team_names(LEAGUE_ID)
    roster_ids = list(names.keys())
    # keep a stable order (by roster_id) so a team's row identity is consistent frame to frame
    roster_ids.sort(key=lambda x: int(x))

    if not snapshots:
        print(f"No snapshots yet for week {week}; nothing to render.")
        return None

    players = common.load_players_cache()
    players_team = common.load_player_teams()
    frames = build_frames(snapshots, roster_ids, players_team)

    # resolve player labels for flashes actually used, so the page payload stays small
    used_pids = {f["pid"] for fr in frames for f in fr["flashes"]}
    player_labels = {pid: players.get(pid, pid) for pid in used_pids}

    # The week's 5 biggest individual point swings, wherever they landed in
    # the timeline — surfaced as clickable markers on the scrubber. Ranked
    # purely by delta size across every flash, positive swings only (a
    # costly INT or fumble isn't modeled as a "big play" here, just the
    # exciting kind). Known simplification: two of the week's top 5 plays
    # landing in the same poll window will render as overlapping
    # dots — rare enough at a few minutes' polling cadence that it isn't worth
    # de-overlapping.
    all_flashes = []
    for idx, fr in enumerate(frames):
        for fl in fr["flashes"]:
            all_flashes.append({"frame": idx, "ts": fr["ts"], "id": fl["id"], "pid": fl["pid"], "delta": fl["delta"]})
    all_flashes.sort(key=lambda f: f["delta"], reverse=True)
    big_plays = [
        {
            "frame": f["frame"],
            "ts": f["ts"],
            "team": names.get(f["id"], f["id"]),
            "player": player_labels.get(f["pid"], f["pid"]),
            "delta": f["delta"],
        }
        for f in all_flashes[:5]
    ]

    # The "week-winning play": the moment the roster that finished #1 in
    # actual points first pushed its own running actual total past the
    # #2 finisher's FINAL actual total (not #2's running total at that
    # moment) — the instant #1 became mathematically uncatchable, since
    # actual points only accumulate over the course of a week and never
    # go back down. A dead-even tie for the final score has no such
    # moment and gets no marker.
    final_teams = frames[-1]["teams"]
    ranked_final = sorted(final_teams, key=lambda t: t["actual"], reverse=True)
    week_winning = None
    winning_key = None
    if len(ranked_final) >= 2:
        top_id = ranked_final[0]["id"]
        second_id = ranked_final[1]["id"]
        second_final_actual = ranked_final[1]["actual"]
        for idx, fr in enumerate(frames):
            by_id = {t["id"]: t for t in fr["teams"]}
            top_pts = by_id.get(top_id, {"actual": 0})["actual"]
            if top_pts > second_final_actual:
                week_winning = {
                    "frame": idx,
                    "ts": fr["ts"],
                    "team": names.get(top_id, top_id),
                    "runnerUp": names.get(second_id, second_id),
                }
                winning_key = (idx, top_id)
                break

    # Tag each frame's flashes so the per-bar popup (see the JS `race-flash`
    # rendering) can prefix a flash that's also one of the week's 5 biggest
    # plays, or the specific flash in the week-winning roster's winning
    # frame — same classification already used for the scrubber markers
    # above, just surfaced on the bar popup too.
    big_play_keys = {(f["frame"], f["id"], f["pid"]) for f in all_flashes[:5]}
    for idx, fr in enumerate(frames):
        for fl in fr["flashes"]:
            key = (idx, fl["id"], fl["pid"])
            if key in big_play_keys:
                fl["kind"] = "big"
            elif winning_key and (idx, fl["id"]) == winning_key:
                fl["kind"] = "winning"

    payload = {
        "week": week,
        "teamNames": [names[rid] for rid in roster_ids],
        "rosterIds": roster_ids,
        "frames": frames,
        "playerLabels": player_labels,
        "bigPlays": big_plays,
        "weekWinning": week_winning,
    }

    html = TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, separators=(",", ":")))
    html = html.replace("__WEEK__", str(week))
    html = html.replace("__SERIES_LIGHT__", json.dumps(SERIES_LIGHT))
    html = html.replace("__SERIES_DARK__", json.dumps(SERIES_DARK))
    html = html.replace("__LAST_UPDATED__", snapshots[-1]["ts"])
    html = html.replace("__LEAGUE_ID__", LEAGUE_ID)

    os.makedirs(DOCS_DIR, exist_ok=True)
    out_path = os.path.join(DOCS_DIR, f"week{week}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Rendered {out_path} from {len(snapshots)} snapshots.")
    return out_path


def render_index():
    os.makedirs(DOCS_DIR, exist_ok=True)
    weeks = []
    for fname in os.listdir(DOCS_DIR):
        m = re.match(r"week(\d+)\.html$", fname)
        if m:
            weeks.append(int(m.group(1)))
    weeks.sort(reverse=True)

    items = "\n".join(f'<li><a href="week{w}.html">Week {w}</a></li>' for w in weeks) or "<li>No weeks played yet.</li>"
    html = INDEX_TEMPLATE.replace("__ITEMS__", items)
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


TEMPLATE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>Week __WEEK__ — Race for Weekly Top Scorer</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@700;800&family=Public+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
  :root{
    color-scheme: light;
    --surface:#f6f5f0; --surface-raised:#ffffff; --surface-sunken:#ebe9e1;
    --ink:#17181a; --ink-secondary:#54544c; --ink-muted:#8b897e;
    --border:rgba(23,24,26,0.11); --accent:#1f6b3d; --accent-ink:#ffffff;
    --live:#c96a12; --live-surface:#f7ead6;
    --winner:#a8790a;
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      color-scheme: dark;
      --surface:#131311; --surface-raised:#1c1c19; --surface-sunken:#0f0f0d;
      --ink:#f1efe6; --ink-secondary:#c4c2b6; --ink-muted:#8b897e;
      --border:rgba(241,239,230,0.12); --accent:#4fae74; --accent-ink:#0d140f;
      --live:#f0ac3f; --live-surface:#362a15;
      --winner:#e6b93d;
    }
  }
  :root[data-theme="dark"]{
    color-scheme: dark;
    --surface:#131311; --surface-raised:#1c1c19; --surface-sunken:#0f0f0d;
    --ink:#f1efe6; --ink-secondary:#c4c2b6; --ink-muted:#8b897e;
    --border:rgba(241,239,230,0.12); --accent:#4fae74; --accent-ink:#0d140f;
    --live:#f0ac3f; --live-surface:#362a15;
    --winner:#e6b93d;
  }
  *{box-sizing:border-box;}
  body{ margin:0; background:var(--surface); color:var(--ink); font-family:"Public Sans",system-ui,sans-serif; -webkit-font-smoothing:antialiased; }
  .wrap{ max-width:860px; margin:0 auto; padding:2.5rem 1.25rem 4rem; }
  .display{ font-family:"Big Shoulders Display",sans-serif; font-weight:800; }
  .mono{ font-family:"IBM Plex Mono",ui-monospace,monospace; font-variant-numeric:tabular-nums; }
  a{ color:var(--accent); }
  .eyebrow{ font-size:0.76rem; font-weight:700; letter-spacing:0.1em; text-transform:uppercase; color:var(--accent); margin-bottom:0.6rem; }
  h1{ font-size:clamp(2rem,5vw,3rem); line-height:0.98; margin:0 0 1.6rem; }
  .race-card{ background:var(--surface-raised); border:1px solid var(--border); border-radius:12px; padding:1.4rem; }
  .race-head{ display:flex; justify-content:space-between; align-items:center; gap:1rem; margin-bottom:1rem; flex-wrap:wrap; }
  .live-badge{ display:inline-flex; align-items:center; gap:0.4em; background:var(--live-surface); color:var(--live); font-size:0.72rem; font-weight:700; letter-spacing:0.05em; text-transform:uppercase; padding:0.3em 0.65em; border-radius:100px; }
  .live-badge .dot{ width:6px; height:6px; border-radius:50%; background:var(--live); }
  .race-track{ position:relative; margin-bottom:1.6rem; }
  .race-gridlines{ position:absolute; inset:0; pointer-events:none; }
  .race-gridline{ position:absolute; top:0; bottom:0; width:1px; background:var(--border); }
  .race-gridline-label{ position:absolute; top:100%; margin-top:0.4rem; transform:translateX(-50%); font-size:0.68rem; color:var(--ink-muted); white-space:nowrap; }
  .race-row{ position:absolute; left:0; right:0; height:34px; transition: top 450ms cubic-bezier(.4,0,.2,1); display:flex; align-items:center; gap:0.6rem; }
  .race-name{ width:150px; flex:0 0 auto; font-size:0.78rem; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; text-align:right; }
  .race-bar-track{ flex:1 1 auto; position:relative; height:20px; }
  .race-bar-proj{ position:absolute; left:0; top:0; height:100%; border-radius:3px; opacity:0.3; transition: width 450ms cubic-bezier(.4,0,.2,1); }
  .race-bar{ position:absolute; left:0; top:0; height:100%; border-radius:3px; min-width:3px; transition: width 450ms cubic-bezier(.4,0,.2,1); }
  .race-score{ position:absolute; top:50%; transform:translateY(-50%); font-size:0.76rem; font-weight:600; white-space:nowrap; transition: left 450ms; }
  .race-score-proj{ position:absolute; top:50%; transform:translateY(-50%); font-size:0.7rem; font-weight:600; color:var(--ink-muted); white-space:nowrap; transition: left 450ms; }
  .race-flash{ position:absolute; top:-1.05rem; font-size:0.68rem; font-weight:600; color:var(--live); opacity:0; white-space:nowrap; transition:opacity 350ms ease; }
  .race-flash.show{ opacity:1; }
  .race-flash-highlight{ font-weight:800; animation: race-flash-glow 1.4s ease-in-out infinite; }
  @keyframes race-flash-glow{
    0%, 100% { text-shadow: 0 0 3px var(--live); }
    50% { text-shadow: 0 0 8px var(--live), 0 0 14px var(--live); }
  }
  .race-legend{ display:flex; gap:1.2rem; font-size:0.76rem; color:var(--ink-secondary); margin-top:1rem; flex-wrap:wrap; }
  .race-legend .item{ display:flex; align-items:center; gap:0.4em; }
  .race-legend .item.clickable{ cursor:pointer; }
  .race-legend .item.clickable:hover{ color:var(--ink); }
  .race-legend .swatch{ width:13px; height:9px; border-radius:2px; background:var(--ink-muted); }
  .race-legend .swatch.proj{ opacity:0.32; }
  .race-legend .swatch.dot{ width:9px; height:9px; border-radius:50%; }
  .race-legend .swatch.star{ width:11px; height:11px; background:var(--winner); clip-path: polygon(50% 0%, 61% 35%, 98% 35%, 68% 57%, 79% 91%, 50% 70%, 21% 91%, 32% 57%, 2% 35%, 39% 35%); }
  .meta{ font-size:0.8rem; color:var(--ink-muted); margin-top:1rem; line-height:1.6; }
  footer{ margin-top:2.5rem; font-size:0.8rem; color:var(--ink-muted); }

  .race-controls{ display:flex; align-items:center; gap:0.8rem; margin-top:1.1rem; padding-top:1rem; border-top:1px solid var(--border); }
  .play-btn{ width:32px; height:32px; border-radius:50%; border:1px solid var(--border); background:var(--surface-sunken); color:var(--ink); cursor:pointer; display:flex; align-items:center; justify-content:center; flex:0 0 auto; }
  .play-btn:hover{ background:var(--accent); color:var(--accent-ink); border-color:var(--accent); }
  .scrub-wrap{ position:relative; flex:1 1 auto; display:flex; align-items:center; }
  .scrub{ width:100%; margin:0; appearance:none; height:4px; border-radius:2px; background:var(--surface-sunken); outline:none; cursor:pointer; }
  .scrub::-webkit-slider-thumb{ appearance:none; width:13px; height:13px; border-radius:50%; background:var(--accent); cursor:pointer; border:2px solid var(--surface-raised); }
  .scrub::-moz-range-thumb{ width:13px; height:13px; border-radius:50%; background:var(--accent); cursor:pointer; border:2px solid var(--surface-raised); }
  .scrub-markers{ position:absolute; left:0; right:0; top:50%; height:0; pointer-events:none; }
  .scrub-marker{ position:absolute; top:0; transform:translate(-50%,-50%); width:8px; height:8px; border-radius:50%; background:var(--live); border:1.5px solid var(--surface-raised); cursor:pointer; pointer-events:auto; z-index:1; }
  .scrub-marker:hover{ transform:translate(-50%,-50%) scale(1.4); }
  .scrub-marker.winning{ width:15px; height:15px; background:var(--winner); border:none; border-radius:0; clip-path: polygon(50% 0%, 61% 35%, 98% 35%, 68% 57%, 79% 91%, 50% 70%, 21% 91%, 32% 57%, 2% 35%, 39% 35%); filter: drop-shadow(0 0 1px var(--surface-raised)); z-index:2; }
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">Week __WEEK__ &middot; <a href="index.html">all weeks</a></div>
  <h1 class="display">Race for Weekly Top Scorer</h1>
  <div class="race-card">
    <div class="race-head">
      <span class="mono" id="clockLabel" style="font-size:0.9rem;font-weight:600;"></span>
      <span class="live-badge"><span class="dot"></span>Updated __LAST_UPDATED__ UTC</span>
    </div>
    <div class="race-track" id="raceTrack"></div>
    <div class="race-controls">
      <button class="play-btn" id="playBtn" aria-label="Play">
        <svg id="playIcon" width="12" height="12" viewBox="0 0 14 14" fill="currentColor"><path d="M2 1.5 12 7 2 12.5Z"/></svg>
        <svg id="pauseIcon" width="12" height="12" viewBox="0 0 14 14" fill="currentColor" style="display:none"><path d="M2.5 1.5h3v11h-3ZM8.5 1.5h3v11h-3Z"/></svg>
      </button>
      <div class="scrub-wrap">
        <input type="range" class="scrub" id="scrub" min="0" max="0" value="0" step="1">
        <div class="scrub-markers" id="scrubMarkers"></div>
      </div>
    </div>
    <div class="race-legend">
      <span class="item"><span class="swatch"></span>Actual points</span>
      <span class="item"><span class="swatch proj"></span>Live-projected final</span>
      <span class="item clickable" id="legendBigPlay"><span class="swatch dot" style="background:var(--live)"></span>Big play (click to jump)</span>
      <span class="item clickable" id="legendWeekWinning"><span class="swatch star"></span>Week-winning play</span>
    </div>
  </div>
  <p class="meta">Teams are ranked by live-projected final (the pale bar), not actual points banked so far — a team with a big head start from players already done can still sit below one with more real upside left on the field. Rebuilt automatically from Sleeper API snapshots polled every few minutes during game windows. Opens showing the latest snapshot — drag the scrubber back to replay how the day got there. Reload for the newest data; this page doesn't auto-refresh itself.</p>
  <footer>Race for Weekly Top Scorer &middot; league <span class="mono">__LEAGUE_ID__</span></footer>
</div>
<script>
(function(){
  "use strict";
  var DATA = __PAYLOAD__;
  var SERIES_LIGHT = __SERIES_LIGHT__;
  var SERIES_DARK = __SERIES_DARK__;
  var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  var SERIES = dark ? SERIES_DARK : SERIES_LIGHT;

  // Chronological context label ("Sunday 1 PM Slate", "Monday Night
  // Football", ...) derived purely from each frame's ET day/hour — a
  // friendly approximation of the real broadcast windows, not a lookup
  // against the actual NFL schedule.
  function slateLabel(d){
    var parts = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", hour: "numeric", hour12: false }).formatToParts(d);
    var weekday = "", hour = 0;
    parts.forEach(function(p){
      if (p.type === "weekday") weekday = p.value;
      if (p.type === "hour") hour = parseInt(p.value, 10);
    });
    if (hour === 24) hour = 0;
    if (weekday === "Thu") return "Thursday Night Football";
    if (weekday === "Fri") return hour < 18 ? "Friday Afternoon Football" : "Friday Night Football";
    if (weekday === "Sat") return hour < 18 ? "Saturday Afternoon Football" : "Saturday Night Football";
    if (weekday === "Sun") return hour < 16 ? "Sunday 1 PM Slate" : (hour < 20 ? "Sunday 4:25 PM Slate" : "Sunday Night Football");
    if (weekday === "Mon") return "Monday Night Football";
    if (weekday === "Tue") return "Tuesday Night Football";
    if (weekday === "Wed") return "Wednesday Night Football";
    return "";
  }

  var N = DATA.rosterIds.length;
  var frames = DATA.frames;
  if (!frames.length){ document.getElementById("raceTrack").textContent = "No data yet."; return; }

  var maxScore = 0;
  frames.forEach(function(fr){ fr.teams.forEach(function(t){ if (t.projected > maxScore) maxScore = t.projected; }); });
  maxScore = Math.max(maxScore * 1.1, 10);

  var track = document.getElementById("raceTrack");
  var rowH = 34 + 6;
  track.style.height = (N * rowH - 6) + "px";

  // Drawn first so it paints behind the bar rows appended below.
  var gridlinesEl = document.createElement("div");
  gridlinesEl.className = "race-gridlines";
  track.appendChild(gridlinesEl);

  var rows = DATA.rosterIds.map(function(rid, i){
    var row = document.createElement("div");
    row.className = "race-row";
    var color = SERIES[i % SERIES.length];
    row.innerHTML =
      '<div class="race-name">' + DATA.teamNames[i] + '</div>' +
      '<div class="race-bar-track">' +
        '<div class="race-bar-proj" style="background:' + color + '"></div>' +
        '<div class="race-bar" style="background:' + color + '"></div>' +
        '<div class="race-flash"></div>' +
        '<div class="race-score mono"></div>' +
        '<div class="race-score-proj mono"></div>' +
      '</div>';
    track.appendChild(row);
    return {
      id: rid,
      el: row,
      barProj: row.querySelector(".race-bar-proj"),
      bar: row.querySelector(".race-bar"),
      score: row.querySelector(".race-score"),
      scoreProj: row.querySelector(".race-score-proj"),
      flash: row.querySelector(".race-flash")
    };
  });

  // Round a raw axis step up to a "nice" 1/2/5 * 10^n value, the standard
  // trick for picking chart tick spacing that reads as round numbers
  // (e.g. 20/40/60, never 17/34/51).
  function niceStep(raw){
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var norm = raw / mag;
    var niceNorm = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
    return niceNorm * mag;
  }
  function computeTicks(maxVal, targetCount){
    var step = niceStep(maxVal / targetCount);
    var ticks = [];
    for (var v = step; v <= maxVal + 1e-6; v += step) ticks.push(Math.round(v * 10) / 10);
    return ticks;
  }

  // Positions the point-value axis under the bars by measuring where the
  // first row's own bar-track actually sits — every row shares the same
  // flex sizing (fixed-width team name + flexible bar area), so one
  // measurement lines up with all of them, and re-measuring on resize
  // keeps it aligned as the page reflows.
  function layoutAxis(){
    gridlinesEl.innerHTML = "";
    if (!rows.length) return;
    var trackRect = track.getBoundingClientRect();
    var barRect = rows[0].bar.parentNode.getBoundingClientRect();
    var left = barRect.left - trackRect.left;
    var width = barRect.width;
    computeTicks(maxScore, 7).forEach(function(v){
      var pct = v / maxScore;
      if (pct > 1.001) return;
      var x = left + pct * width;
      var line = document.createElement("div");
      line.className = "race-gridline";
      line.style.left = x + "px";
      gridlinesEl.appendChild(line);
      var label = document.createElement("div");
      label.className = "race-gridline-label";
      label.style.left = x + "px";
      label.textContent = Math.round(v);
      gridlinesEl.appendChild(label);
    });
  }
  // A plain resize listener alone can leave the axis pinned at a
  // zero-width measurement forever: if this page first lays out while not
  // yet visible at real size (e.g. a background tab), nothing ever fires
  // a "resize" event once it becomes visible at the same logical window
  // size. ResizeObserver watches the bar-track element itself, so it
  // fires on that transition too, not just on window resizes.
  if (window.ResizeObserver){
    new ResizeObserver(layoutAxis).observe(rows[0].bar.parentNode);
  } else {
    window.addEventListener("resize", layoutAxis);
  }

  function render(idx){
    var f = frames[idx];
    var byId = {};
    f.teams.forEach(function(t){ byId[t.id] = t; });

    // Ranked by live-projected final, not actual-so-far — a team that's
    // banked a big score from players who are already done can still sit
    // below a team with less banked but more real upside left on the field.
    var order = f.teams.slice().sort(function(a,b){ return b.projected - a.projected; }).map(function(t){ return t.id; });
    var rank = {};
    order.forEach(function(id,pos){ rank[id] = pos; });

    rows.forEach(function(r){
      var t = byId[r.id];
      var pos = rank[r.id];
      r.el.style.top = (pos * rowH) + "px";
      var pct = Math.max(1.5, (t.actual / maxScore) * 100);
      var pctProj = Math.max(pct, (t.projected / maxScore) * 100);
      r.bar.style.width = pct + "%";
      r.barProj.style.width = pctProj + "%";
      r.score.textContent = t.actual.toFixed(1);
      r.score.style.left = "calc(" + pct + "% + 8px)";

      // Only label the pale projected bar when it's actually showing
      // meaningful upside above actual — once a team's game is over (or
      // it just hasn't separated from actual yet) the two numbers are the
      // same and a second identical label next to the first is just noise.
      if (t.projected - t.actual > 0.05){
        r.scoreProj.textContent = t.projected.toFixed(1);
        r.scoreProj.style.left = "calc(" + pctProj + "% + 8px)";
      } else {
        r.scoreProj.textContent = "";
      }

      // A roster can have more than one flash in the same frame (two
      // starters both scoring in the same poll window). Prefer one tagged
      // "big"/"winning" over an ordinary one -- otherwise clicking a
      // marker/legend jump could land on the right frame but show a
      // completely different, untagged play for that team instead of the
      // one the marker promised.
      var flashesForRow = f.flashes.filter(function(x){ return x.id === r.id; });
      var fl = flashesForRow.find(function(x){ return x.kind; }) || flashesForRow[0];
      if (fl){
        var label = DATA.playerLabels[fl.pid] || fl.pid;
        var prefixText = fl.kind === "big" ? "Big Play: " : (fl.kind === "winning" ? "Week-Winning Play: " : "");
        var text = prefixText + "+" + fl.delta.toFixed(1) + " · " + label;
        r.flash.innerHTML = prefixText ? '<span class="race-flash-highlight">' + text + '</span>' : text;
        r.flash.classList.add("show");
      } else {
        r.flash.classList.remove("show");
      }
    });

    var d = new Date(f.ts);
    var when = d.toLocaleString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit", month: "short", day: "numeric" });
    document.getElementById("clockLabel").textContent = when + " ET · " + slateLabel(d);
  }

  var scrub = document.getElementById("scrub");
  scrub.max = frames.length - 1;

  // Clickable markers for this week's 5 biggest plays + the week-winning
  // play, positioned along the scrubber by frame index.
  //
  // A native <input type="range">'s thumb is inset from each edge of the
  // track by half its own width -- its center can never actually reach
  // 0% or 100%, only THUMB_RADIUS..(100%-THUMB_RADIUS) -- but a plain
  // "left: X%" on this absolutely-positioned overlay spans the FULL
  // width with no such inset. The two use different position math, so a
  // marker near either end sat visibly off from where the thumb lands
  // after jumping to it (dead-on in the middle, drifting apart toward the
  // edges). leftForFrame mirrors the thumb's own inset geometry via
  // calc() so a marker's dot/star and the thumb it jumps to always align.
  var SCRUB_THUMB_RADIUS = 6.5; // half of the 13px thumb in .scrub::-webkit-slider-thumb / -moz-range-thumb
  var markersEl = document.getElementById("scrubMarkers");
  function leftForFrame(idx){
    var frac = frames.length > 1 ? (idx / (frames.length - 1)) : 0;
    return "calc(" + SCRUB_THUMB_RADIUS + "px + (100% - " + (SCRUB_THUMB_RADIUS * 2) + "px) * " + frac + ")";
  }
  function jumpTo(frameIdx){
    setPlaying(false);
    clearInterval(timer);
    current = frameIdx;
    scrub.value = frameIdx;
    render(frameIdx);
  }
  function addMarker(frameIdx, title, cls){
    var m = document.createElement("div");
    m.className = "scrub-marker" + (cls ? " " + cls : "");
    m.style.left = leftForFrame(frameIdx);
    m.title = title;
    m.addEventListener("click", function(){ jumpTo(frameIdx); });
    markersEl.appendChild(m);
  }
  var bigPlays = DATA.bigPlays || [];
  bigPlays.forEach(function(p){
    addMarker(p.frame, "Big Play: +" + p.delta.toFixed(1) + " · " + p.player + " (" + p.team + ")");
  });
  if (DATA.weekWinning){
    addMarker(DATA.weekWinning.frame, "Week-Winning Play: " + DATA.weekWinning.team + " passes " + DATA.weekWinning.runnerUp + "'s final score for good", "winning");
  }

  // The legend entries double as jump shortcuts, matching the "(click to
  // jump)" text: "Big play" jumps to the single biggest play of the week
  // (bigPlays is already sorted by delta, descending); "Week-winning play"
  // jumps to that specific moment. Neither is clickable if the week has
  // no such moment yet (e.g. early in the game).
  var legendBigPlay = document.getElementById("legendBigPlay");
  if (bigPlays.length){
    legendBigPlay.addEventListener("click", function(){ jumpTo(bigPlays[0].frame); });
  } else {
    legendBigPlay.classList.remove("clickable");
  }
  var legendWeekWinning = document.getElementById("legendWeekWinning");
  if (DATA.weekWinning){
    legendWeekWinning.addEventListener("click", function(){ jumpTo(DATA.weekWinning.frame); });
  } else {
    legendWeekWinning.classList.remove("clickable");
  }

  var current = frames.length - 1;
  var playing = false;
  var timer = null;

  function setPlaying(p){
    playing = p;
    document.getElementById("playIcon").style.display = p ? "none" : "";
    document.getElementById("pauseIcon").style.display = p ? "" : "none";
  }

  function tick(){
    if (!playing) return;
    current++;
    if (current >= frames.length){ current = frames.length - 1; setPlaying(false); clearInterval(timer); }
    scrub.value = current;
    render(current);
  }

  document.getElementById("playBtn").addEventListener("click", function(){
    if (current >= frames.length - 1){ current = 0; scrub.value = 0; render(0); }
    setPlaying(!playing);
    clearInterval(timer);
    if (playing) timer = setInterval(tick, 200);
  });

  scrub.addEventListener("input", function(){
    setPlaying(false);
    clearInterval(timer);
    current = parseInt(scrub.value, 10);
    render(current);
  });

  scrub.value = current;
  render(current); // show the latest snapshot by default; drag the scrubber to replay the day
  layoutAxis();
})();
</script>
</body>
</html>
"""

INDEX_TEMPLATE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>Race for Weekly Top Scorer</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  body{ font-family: system-ui, sans-serif; background:#f6f5f0; color:#17181a; max-width:640px; margin:3rem auto; padding:0 1.25rem; }
  h1{ font-size:1.8rem; }
  ul{ padding-left: 1.2rem; line-height: 2; }
  a{ color:#1f6b3d; font-weight:600; }
</style>
</head>
<body>
  <h1>Race for Weekly Top Scorer</h1>
  <ul>
__ITEMS__
  </ul>
</body>
</html>
"""

if __name__ == "__main__":
    # Optional explicit week, e.g. `python scripts/render.py 0` to render the
    # dummy-data test week regardless of what's actually live right now.
    if len(sys.argv) > 1:
        week = int(sys.argv[1])
    else:
        state = common.get_state()
        week = state.get("display_week") or state["week"]
    render_week(week)
    render_index()
